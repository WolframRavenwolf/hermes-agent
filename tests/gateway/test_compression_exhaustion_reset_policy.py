"""Behavioral coverage for compression exhaustion versus reset policy."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource


_EXHAUSTION_MARKER = "compression_exhausted"


def _event_and_source():
    source = SessionSource(
        platform=Platform.MATTERMOST,
        chat_id="channel-1",
        chat_type="channel",
        thread_id="root-1",
        user_id="wolfram",
    )
    return MessageEvent(text="continue", source=source, message_id="post-1"), source


def _make_runner(mode: str, *, exhausted: bool = False):
    config = GatewayConfig(
        default_reset_policy=SessionResetPolicy(mode=mode)
    )
    runner = GatewayRunner(config)
    runner.adapters = {}
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
        metadata={_EXHAUSTION_MARKER: True} if exhausted else {},
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
    runner.session_store.set_session_metadata = MagicMock(return_value=True)
    return runner, old_entry


def _exhausted_result(completion):
    error = "Context length exceeded: max compression attempts (3) reached."
    return {
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


@pytest.mark.parametrize("mode", ["none", "idle", "daily", "both"])
@pytest.mark.asyncio
async def test_compression_exhaustion_obeys_reset_policy(mode):
    runner, old_entry = _make_runner(mode)
    event, source = _event_and_source()
    completion = AsyncMock(return_value=False)
    runner._run_agent = AsyncMock(return_value=_exhausted_result(completion))

    response = await runner._handle_message_with_agent(
        event, source, old_entry.session_key, 1
    )

    completion.assert_awaited_once()
    assert getattr(event, "_gateway_skip_goal_continuation") is True
    terminal_text = completion.await_args.args[0]
    assert response == terminal_text
    if mode == "none":
        runner.session_store.reset_session.assert_not_called()
        runner.session_store.set_session_metadata.assert_called_once_with(
            old_entry.session_key, _EXHAUSTION_MARKER, True
        )
        runner._clear_conversation_scope.assert_not_called()
        assert "session preserved" in terminal_text.lower()
        assert "automatic model retries are paused" in terminal_text.lower()
        assert "/compress" in terminal_text
        assert "/new" in terminal_text
        assert "auto-reset" not in terminal_text.lower()
    else:
        runner.session_store.reset_session.assert_called_once_with(
            old_entry.session_key
        )
        runner.session_store.set_session_metadata.assert_not_called()
        runner._clear_conversation_scope.assert_called_once_with(
            old_entry.session_key, reason="compression_exhausted_reset"
        )
        assert "session auto-reset" in terminal_text.lower()


@pytest.mark.asyncio
async def test_known_exhausted_session_pauses_before_agent_invocation():
    runner, old_entry = _make_runner("none", exhausted=True)
    event, source = _event_and_source()
    runner._run_agent = AsyncMock()

    response = await runner._handle_message_with_agent(
        event, source, old_entry.session_key, 1
    )

    runner._run_agent.assert_not_awaited()
    assert getattr(event, "_gateway_skip_goal_continuation") is True
    runner.session_store.load_transcript.assert_not_called()
    assert "automatic model retries are paused" in response.lower()
    assert "/compress" in response
    assert "/new" in response


@pytest.mark.parametrize("mode", ["idle", "daily", "both"])
@pytest.mark.asyncio
async def test_known_exhausted_session_honors_later_reset_opt_in(mode):
    runner, old_entry = _make_runner(mode, exhausted=True)
    event, source = _event_and_source()
    runner._run_agent = AsyncMock()

    response = await runner._handle_message_with_agent(
        event, source, old_entry.session_key, 1
    )

    runner._run_agent.assert_not_awaited()
    assert getattr(event, "_gateway_skip_goal_continuation") is True
    runner.session_store.load_transcript.assert_not_called()
    runner.session_store.reset_session.assert_called_once_with(
        old_entry.session_key
    )
    runner._clear_conversation_scope.assert_called_once_with(
        old_entry.session_key, reason="compression_exhausted_reset"
    )
    assert "session auto-reset" in response.lower()


@pytest.mark.asyncio
async def test_exhaustion_notice_does_not_schedule_goal_continuation(monkeypatch):
    runner, _old_entry = _make_runner("none")
    event, _source = _event_and_source()
    notice = "compression exhausted; use /compress or /new"

    async def _exhausted_notice(current_event, *_args):
        setattr(current_event, "_gateway_skip_goal_continuation", True)
        return notice

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_a, **_kw: [])
    runner._scale_to_zero_note_real_inbound = MagicMock()
    runner._claim_active_session_slot = MagicMock(return_value=(None, None))
    runner._persist_active_agents = MagicMock()
    runner._handle_message_with_agent = AsyncMock(side_effect=_exhausted_notice)
    runner._post_turn_goal_continuation = AsyncMock()
    runner._restore_moa_one_shot = MagicMock()
    runner._restore_pending_one_turn_model_override = MagicMock()
    runner._release_running_agent_state = MagicMock()
    runner._release_turn_lease = MagicMock()

    response = await runner._handle_message(event)

    assert response == notice
    runner._post_turn_goal_continuation.assert_not_awaited()
