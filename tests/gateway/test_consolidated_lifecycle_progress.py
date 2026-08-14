"""Regression tests for consolidated gateway lifecycle progress.

These tests pin the missing protocols around the existing accumulated progress
rail: mutable heartbeat upserts, acknowledged terminal lifecycle delivery, and
producer-before-consumer shutdown ordering.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.platforms.base import MessageEvent, SendResult
from gateway.run import GatewayRunner, TurnRunner
from gateway.session import SessionEntry, SessionSource
from gateway.turn_context import TurnContext


HEARTBEAT_KEY = "long_running_heartbeat"


class CaptureAdapter:
    name = "capture"
    MAX_MESSAGE_LENGTH = 4000
    SUPPORTS_MESSAGE_EDITING = True
    REQUIRES_EDIT_FINALIZE = False

    def __init__(self, *, fail_edits: bool = False):
        self.fail_edits = fail_edits
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.typing: list[dict | None] = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="progress-1")

    async def edit_message(self, chat_id, message_id, content):
        self.edits.append(content)
        if self.fail_edits:
            return SendResult(success=False, error="synthetic edit failure")
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None):
        self.typing.append(metadata)


class CaptureRunner:
    def __init__(self, adapter):
        self.adapter = adapter

    def _adapter_for_source(self, source):
        return self.adapter


def _make_turn(
    adapter,
    *,
    max_message_length: int | None = None,
    run_still_current=lambda: True,
    agent=None,
):
    if max_message_length is not None:
        adapter.MAX_MESSAGE_LENGTH = max_message_length
    ctx = TurnContext(
        source=SessionSource(
            platform=Platform.MATTERMOST,
            chat_id="channel-1",
            chat_type="channel",
            thread_id="root-1",
        ),
        _run_still_current=run_still_current,
        progress_grouping="accumulate",
        progress_queue=queue.Queue(),
        agent_holder=[agent],
    )
    return ctx, TurnRunner(CaptureRunner(adapter), ctx)


async def _drain_progress(turn: TurnRunner):
    task = asyncio.create_task(turn.send_progress_messages())
    await asyncio.sleep(0.05)
    task.cancel()
    await task


@pytest.mark.asyncio
async def test_heartbeat_upsert_replaces_prior_line_and_moves_to_fifo_tail():
    adapter = CaptureAdapter()
    ctx, turn = _make_turn(adapter)

    ctx.progress_queue.put("first tool")
    ctx.progress_queue.put(("__upsert__", HEARTBEAT_KEY, "⏳ Working — 1 min"))
    ctx.progress_queue.put("second tool")
    ctx.progress_queue.put(("__upsert__", HEARTBEAT_KEY, "⏳ Working — 2 min"))

    await _drain_progress(turn)

    final = (adapter.edits or adapter.sent)[-1].splitlines()
    assert final == ["first tool", "second tool", "⏳ Working — 2 min"]
    assert "⏳ Working — 1 min" not in "\n".join(adapter.sent + adapter.edits)


@pytest.mark.asyncio
async def test_upsert_uses_newest_editable_bubble_after_rollover():
    adapter = CaptureAdapter()
    ctx, turn = _make_turn(adapter, max_message_length=42)

    ctx.progress_queue.put("first-completed-bubble-line")
    ctx.progress_queue.put(("__upsert__", HEARTBEAT_KEY, "⏳ Working — 1 min"))
    ctx.progress_queue.put("second-bubble-line")
    ctx.progress_queue.put(("__upsert__", HEARTBEAT_KEY, "⏳ Working — 2 min"))

    await _drain_progress(turn)

    assert len(adapter.sent) >= 2
    newest = (adapter.edits or adapter.sent)[-1]
    rendered = "\n".join(adapter.sent + adapter.edits)
    assert "__upsert__" not in rendered
    assert "long_running_heartbeat" not in rendered
    assert "⏳ Working — 2 min" in newest
    assert "⏳ Working — 1 min" not in newest
    assert "first-completed-bubble-line" not in newest


@pytest.mark.asyncio
async def test_rollover_success_without_message_id_retires_delivered_continuation():
    class NoIdContinuationAdapter(CaptureAdapter):
        MAX_MESSAGE_LENGTH = 30

        def __init__(self):
            super().__init__()
            self.send_count = 0
            self.later_delivered = asyncio.Event()

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(content)
            self.send_count += 1
            if "later" in content:
                self.later_delivered.set()
            message_id = None if self.send_count == 2 else f"progress-{self.send_count}"
            return SendResult(success=True, message_id=message_id)

        async def edit_message(self, chat_id, message_id, content):
            self.edits.append(content)
            if "later" in content:
                self.later_delivered.set()
            return SendResult(success=True, message_id=message_id)

    adapter = NoIdContinuationAdapter()
    ctx, turn = _make_turn(adapter, max_message_length=30)
    ack = asyncio.get_running_loop().create_future()
    ctx.progress_queue.put("first-bubble-content")
    ctx.progress_queue.put(("__terminal__", "terminal-notice", ack))
    task = asyncio.create_task(turn.send_progress_messages())

    assert await asyncio.wait_for(ack, timeout=2.0) is True
    ctx.progress_queue.put("later")
    await asyncio.wait_for(adapter.later_delivered.wait(), timeout=2.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert adapter.sent.count("terminal-notice") == 1
    assert all("terminal-notice" not in content for content in adapter.edits)


@pytest.mark.asyncio
async def test_rollover_keeps_keyed_heartbeat_in_newest_mutable_bubble():
    class StatefulRolloverAdapter(CaptureAdapter):
        MAX_MESSAGE_LENGTH = 24

        def __init__(self):
            super().__init__()
            self.next_id = 0
            self.message_order: list[str] = []
            self.messages: dict[str, str] = {}
            self.first_send_finished = asyncio.Event()
            self.second_send_finished = asyncio.Event()
            self.latest_heartbeat_rendered = asyncio.Event()

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(content)
            self.next_id += 1
            message_id = f"progress-{self.next_id}"
            self.message_order.append(message_id)
            self.messages[message_id] = content
            if self.next_id == 1:
                self.first_send_finished.set()
            elif self.next_id == 2:
                self.second_send_finished.set()
            if "heartbeat-two" in content:
                self.latest_heartbeat_rendered.set()
            return SendResult(success=True, message_id=message_id)

        async def edit_message(self, chat_id, message_id, content):
            self.edits.append(content)
            self.messages[message_id] = content
            if "heartbeat-two" in content:
                self.latest_heartbeat_rendered.set()
            return SendResult(success=True, message_id=message_id)

        def visible_text(self) -> str:
            return "\n".join(self.messages[mid] for mid in self.message_order)

    adapter = StatefulRolloverAdapter()
    ctx, turn = _make_turn(adapter, max_message_length=24)
    ctx.progress_queue.put(("__upsert__", HEARTBEAT_KEY, "heartbeat-one"))
    task = asyncio.create_task(turn.send_progress_messages())

    await asyncio.wait_for(adapter.first_send_finished.wait(), timeout=2.0)
    ctx.progress_queue.put("ordinary-progress-line")
    await asyncio.wait_for(adapter.second_send_finished.wait(), timeout=2.0)
    ctx.progress_queue.put(("__upsert__", HEARTBEAT_KEY, "heartbeat-two"))
    await asyncio.wait_for(adapter.latest_heartbeat_rendered.wait(), timeout=2.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    visible = adapter.visible_text()
    assert "heartbeat-two" in visible
    assert "heartbeat-one" not in visible


@pytest.mark.asyncio
async def test_rollover_first_group_send_failure_preserves_complete_retry_buffer():
    class FailingFreshRolloverAdapter(CaptureAdapter):
        MAX_MESSAGE_LENGTH = 24

        def __init__(self):
            super().__init__()
            self.send_count = 0
            self.message_order: list[str] = []
            self.messages: dict[str, str] = {}
            self.first_failure = asyncio.Event()
            self.first_group_failure = asyncio.Event()
            self.later_group_failure = asyncio.Event()
            self.trigger_delivered = asyncio.Event()

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(content)
            self.send_count += 1
            if self.send_count in {1, 2, 4}:
                if self.send_count == 1:
                    self.first_failure.set()
                elif self.send_count == 2:
                    self.first_group_failure.set()
                else:
                    self.later_group_failure.set()
                return SendResult(success=False, error="synthetic send failure")
            message_id = f"progress-{self.send_count}"
            self.message_order.append(message_id)
            self.messages[message_id] = content
            if "retry-trigger" in content:
                self.trigger_delivered.set()
            return SendResult(success=True, message_id=message_id)

        async def edit_message(self, chat_id, message_id, content):
            self.edits.append(content)
            self.messages[message_id] = content
            if "retry-trigger" in content:
                self.trigger_delivered.set()
            return SendResult(success=True, message_id=message_id)

        def visible_text(self) -> str:
            return "\n".join(self.messages[mid] for mid in self.message_order)

    adapter = FailingFreshRolloverAdapter()
    ctx, turn = _make_turn(adapter, max_message_length=24)
    task = asyncio.create_task(turn.send_progress_messages())
    ctx.progress_queue.put("first-pending-content")
    await asyncio.wait_for(adapter.first_failure.wait(), timeout=2.0)

    ctx.progress_queue.put("second-pending-content")
    await asyncio.wait_for(adapter.first_group_failure.wait(), timeout=2.0)
    ctx.progress_queue.put("third-pending-content")
    await asyncio.wait_for(adapter.later_group_failure.wait(), timeout=2.0)
    ctx.progress_queue.put("retry-trigger")
    await asyncio.wait_for(adapter.trigger_delivered.wait(), timeout=2.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    visible = adapter.visible_text()
    assert "first-pending-content" in visible
    assert "second-pending-content" in visible
    assert "third-pending-content" in visible
    assert "retry-trigger" in visible


@pytest.mark.asyncio
async def test_rollover_keeps_terminal_boundary_after_keyed_heartbeat():
    class StatefulTerminalAdapter(CaptureAdapter):
        MAX_MESSAGE_LENGTH = 24

        def __init__(self):
            super().__init__()
            self.next_id = 0
            self.message_order: list[str] = []
            self.messages: dict[str, str] = {}
            self.second_send_finished = asyncio.Event()

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(content)
            self.next_id += 1
            message_id = f"progress-{self.next_id}"
            self.message_order.append(message_id)
            self.messages[message_id] = content
            if self.next_id == 2:
                self.second_send_finished.set()
            return SendResult(success=True, message_id=message_id)

        async def edit_message(self, chat_id, message_id, content):
            self.edits.append(content)
            self.messages[message_id] = content
            return SendResult(success=True, message_id=message_id)

        def visible_lines(self) -> list[str]:
            return "\n".join(
                self.messages[mid] for mid in self.message_order
            ).splitlines()

    adapter = StatefulTerminalAdapter()
    ctx, turn = _make_turn(adapter, max_message_length=24)
    ctx.progress_queue.put(("__upsert__", HEARTBEAT_KEY, "heartbeat-one"))
    ack = asyncio.get_running_loop().create_future()
    ctx.progress_queue.put(("__terminal__", "terminal-notice", ack))
    task = asyncio.create_task(turn.send_progress_messages())

    assert await asyncio.wait_for(ack, timeout=2.0) is True
    await asyncio.wait_for(adapter.second_send_finished.wait(), timeout=2.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    visible = adapter.visible_lines()
    assert visible.index("heartbeat-one") < visible.index("terminal-notice")


@pytest.mark.asyncio
async def test_terminal_lifecycle_ack_is_true_only_after_successful_edit():
    adapter = CaptureAdapter()
    ctx, turn = _make_turn(adapter)

    ctx.progress_queue.put("tool before failure")
    ack = asyncio.get_running_loop().create_future()
    ctx.progress_queue.put(
        ("__terminal__", "Context length exceeded.\n\n🔄 Session auto-reset.", ack)
    )

    await _drain_progress(turn)

    assert ack.done()
    assert ack.result() is True
    final = (adapter.edits or adapter.sent)[-1]
    assert final.splitlines() == [
        "tool before failure",
        "Context length exceeded.",
        "",
        "🔄 Session auto-reset.",
    ]


@pytest.mark.asyncio
async def test_terminal_lifecycle_ack_is_false_when_edit_and_fallback_send_fail():
    class FailingAdapter(CaptureAdapter):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(content)
            if content == "tool before failure":
                return SendResult(success=True, message_id="progress-1")
            return SendResult(success=False, error="synthetic send failure")

    adapter = FailingAdapter(fail_edits=True)
    ctx, turn = _make_turn(adapter)

    ctx.progress_queue.put("tool before failure")
    ack = asyncio.get_running_loop().create_future()
    ctx.progress_queue.put(("__terminal__", "Context length exceeded.", ack))

    await _drain_progress(turn)

    assert ack.done()
    assert ack.result() is False


@pytest.mark.asyncio
async def test_terminal_lifecycle_success_without_message_id_is_delivered():
    class NoMessageIdAdapter(CaptureAdapter):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(content)
            return SendResult(success=True, message_id=None)

    adapter = NoMessageIdAdapter()
    ctx, turn = _make_turn(adapter)
    ack = asyncio.get_running_loop().create_future()
    ctx.progress_queue.put(("__terminal__", "Context length exceeded.", ack))

    await _drain_progress(turn)

    assert ack.done()
    assert ack.result() is True
    assert adapter.sent == ["Context length exceeded."]


@pytest.mark.asyncio
async def test_terminal_lifecycle_stale_run_resolves_not_delivered():
    ownership_checks = iter((True, False))
    adapter = CaptureAdapter()
    ctx, turn = _make_turn(
        adapter,
        run_still_current=lambda: next(ownership_checks, False),
    )
    ack = asyncio.get_running_loop().create_future()
    ctx.progress_queue.put(("__terminal__", "Context length exceeded.", ack))

    await asyncio.wait_for(turn.send_progress_messages(), timeout=1.0)

    assert ack.done()
    assert ack.result() is False
    assert adapter.sent == []
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_terminal_lifecycle_interrupt_drop_resolves_not_delivered():
    adapter = CaptureAdapter()
    ctx, turn = _make_turn(adapter, agent=SimpleNamespace(is_interrupted=True))
    ack = asyncio.get_running_loop().create_future()
    ctx.progress_queue.put(("__terminal__", "Context length exceeded.", ack))

    task = asyncio.create_task(turn.send_progress_messages())
    assert await asyncio.wait_for(ack, timeout=1.0) is False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert ack.done()
    assert ack.result() is False
    assert adapter.sent == []
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_flush_barrier_delivers_idle_queued_heartbeat_before_shutdown():
    """A producer's last upsert must cross the rail before owner cancellation."""
    adapter = CaptureAdapter()
    ctx, turn = _make_turn(adapter)
    task = asyncio.create_task(turn.send_progress_messages())

    # Let the consumer enter its empty-queue poll before the producer's final
    # event arrives. This is the shutdown race the owner-side barrier closes.
    await asyncio.sleep(0.05)
    ack = asyncio.get_running_loop().create_future()
    ctx.progress_queue.put(
        ("__upsert__", HEARTBEAT_KEY, "⏳ Working — final heartbeat")
    )
    ctx.progress_queue.put(("__flush__", ack))

    assert await asyncio.wait_for(ack, timeout=2.0) is True
    task.cancel()
    await task

    visible = (adapter.edits or adapter.sent)[-1]
    assert visible == "⏳ Working — final heartbeat"


@pytest.mark.asyncio
async def test_idle_consumer_cancellation_drains_late_queued_event():
    """Cancellation in the empty-poll sleep must enter the lossless drain."""
    adapter = CaptureAdapter()
    ctx, turn = _make_turn(adapter)
    task = asyncio.create_task(turn.send_progress_messages())

    # Put the consumer inside the queue.Empty sleep, then race one final
    # already-admitted event with owner cancellation.
    await asyncio.sleep(0.05)
    ctx.progress_queue.put("late ordinary progress")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert ctx.progress_queue.empty()
    assert (adapter.edits or adapter.sent)[-1] == "late ordinary progress"


def test_cleanup_fence_waits_for_callback_that_already_entered():
    """Close must linearize after an already-entered worker callback."""

    class PausingFence:
        def __init__(self):
            self._open = True
            self.checked = threading.Event()
            self.release = threading.Event()

        def is_set(self):
            captured = self._open
            self.checked.set()
            if not self.release.wait(timeout=2.0):
                raise TimeoutError("test did not release callback")
            return captured

        def clear(self):
            self._open = False

    adapter = CaptureAdapter()
    ctx, turn = _make_turn(adapter)
    ctx._thinking_enabled = True
    fence = PausingFence()
    turn._progress_ingress_open = fence
    callback_done = threading.Event()
    close_done = threading.Event()
    errors: list[BaseException] = []

    def run_callback():
        try:
            turn.progress_callback("_thinking", "late thought")
        except BaseException as exc:
            errors.append(exc)
        finally:
            callback_done.set()

    def close_ingress():
        try:
            turn.close_progress_ingress()
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_done.set()

    callback_thread = threading.Thread(target=run_callback)
    close_thread = threading.Thread(target=close_ingress)
    callback_thread.start()
    assert fence.checked.wait(timeout=1.0)
    close_thread.start()
    closed_before_callback_finished = close_done.wait(timeout=0.05)
    fence.release.set()
    callback_thread.join(timeout=1.0)
    close_thread.join(timeout=1.0)

    assert not errors
    assert callback_done.is_set()
    assert close_done.is_set()
    assert not closed_before_callback_finished
    assert list(ctx.progress_queue.queue) == ["💬 late thought"]


def _make_handler_runner(tmp_path, adapter):
    config = GatewayConfig(
        default_reset_policy=SessionResetPolicy(mode="idle")
    )
    runner = GatewayRunner(config)
    runner.adapters = {Platform.MATTERMOST: adapter}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._begin_session_run_generation = lambda _key: 1
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._evict_cached_agent = MagicMock()
    runner._clear_conversation_scope = MagicMock()
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    session_key = "agent:main:mattermost:channel:channel-1:root-1"
    old_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-exhausted",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.MATTERMOST,
        chat_type="channel",
    )
    fresh_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-fresh",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.MATTERMOST,
        chat_type="channel",
    )
    runner.session_store = MagicMock()
    runner.session_store.config = config
    runner.session_store.get_or_create_session.return_value = old_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.reset_session.return_value = fresh_entry
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    return runner, session_key


def _handler_event_and_source():
    source = SessionSource(
        platform=Platform.MATTERMOST,
        chat_id="channel-1",
        chat_type="channel",
        thread_id="root-1",
        user_id="wolfram",
    )
    event = MessageEvent(text="continue", source=source, message_id="post-1")
    return event, source


@pytest.mark.parametrize(
    ("rail_delivered", "expect_fallback"),
    [(True, False), (False, True)],
    ids=["confirmed-rail-suppresses-assistant", "failed-rail-keeps-assistant"],
)
@pytest.mark.asyncio
async def test_compression_exhaustion_suppresses_only_confirmed_progress_delivery(
    monkeypatch, tmp_path, rail_delivered, expect_fallback
):
    adapter = CaptureAdapter()
    runner, session_key = _make_handler_runner(tmp_path, adapter)
    event, source = _handler_event_and_source()
    completion = AsyncMock(return_value=rail_delivered)
    error = "Context length exceeded: max compression attempts (3) reached."
    runner._run_agent = AsyncMock(
        return_value={
            "failed": True,
            "partial": True,
            "compression_exhausted": True,
            "final_response": error,
            "error": error,
            "messages": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "_complete_lifecycle_progress": completion,
        }
    )

    response = await runner._handle_message_with_agent(
        event, source, session_key, 1
    )

    completion.assert_awaited_once()
    terminal_text = completion.await_args.args[0]
    assert error in terminal_text
    assert "🔄 Session auto-reset" in terminal_text
    if expect_fallback:
        assert response == terminal_text
    else:
        assert response == ""


def test_cleanup_stops_heartbeat_producer_before_progress_consumer():
    """Pin the owner ordering without relying on wall-clock timing."""
    import ast
    import inspect
    import textwrap

    import gateway.run as gateway_run

    tree = ast.parse(textwrap.dedent(inspect.getsource(gateway_run.GatewayRunner._run_agent_inner)))
    finally_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Try) and node.finalbody]
    owner_finally = next(
        node.finalbody
        for node in finally_nodes
        if any(
            isinstance(sub, ast.Name)
            and sub.id == "_complete_lifecycle_progress"
            for stmt in node.finalbody
            for sub in ast.walk(stmt)
        )
        and any(
            isinstance(sub, ast.Name) and sub.id == "_notify_task"
            for stmt in node.finalbody
            for sub in ast.walk(stmt)
        )
    )

    def first_statement_index(name: str) -> int:
        for index, stmt in enumerate(owner_finally):
            if any(isinstance(sub, ast.Name) and sub.id == name for sub in ast.walk(stmt)):
                return index
        raise AssertionError(f"{name} not found in owner finally")

    assert first_statement_index("_notify_task") < first_statement_index(
        "_complete_lifecycle_progress"
    )
