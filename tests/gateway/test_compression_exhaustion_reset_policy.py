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
    runner._post_turn_loop_completion = AsyncMock()
    runner._restore_moa_one_shot = MagicMock()
    runner._restore_pending_one_turn_model_override = MagicMock()
    runner._release_running_agent_state = MagicMock()
    runner._release_turn_lease = MagicMock()

    response = await runner._handle_message(event)

    assert response == notice
    runner._post_turn_goal_continuation.assert_not_awaited()
    runner._post_turn_loop_completion.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [True, False])
async def test_persisted_pause_blocks_native_goal_hook_but_releases_loop(durable):
    runner, entry = _make_runner("none", exhausted=durable)
    if not durable:
        entry._compression_pause_pending = True
    event, source = _event_and_source()
    runner._post_turn_goal_continuation = AsyncMock()
    runner._post_turn_loop_completion = AsyncMock()
    await runner._run_post_turn_hooks(
        agent_result="preserved context", source=source, is_internal=False, event=event
    )
    runner._post_turn_goal_continuation.assert_not_awaited()
    runner._post_turn_loop_completion.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["in_place", "rotation", "no_op", "locked", "write_failed", "routing_failed"])
async def test_only_committed_manual_compression_releases_pause(outcome, monkeypatch):
    from tests.gateway.test_compress_command import _make_runner as compress_runner, _make_history, _make_event
    history = _make_history()
    runner = compress_runner(history)
    entry = runner.session_store.get_or_create_session.return_value
    entry.metadata[_EXHAUSTION_MARKER] = True
    agent = MagicMock()
    agent._cached_system_prompt = ""
    agent.tools = None
    agent.session_id = "compressed-child" if outcome in {"rotation", "write_failed", "routing_failed"} else entry.session_id
    agent._last_compaction_in_place = outcome == "in_place"
    agent._compression_skipped_due_to_lock = outcome == "locked"
    agent._compress_context.return_value = ([history[0], history[-1]], "")
    runner.session_store.rewrite_transcript.return_value = outcome != "write_failed"
    runner.session_store._save.return_value = outcome != "routing_failed"
    monkeypatch.setattr("run_agent.AIAgent", lambda **kwargs: agent)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {"api_key": "fixture"})
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda *a: "fixture")
    monkeypatch.setattr("agent.model_metadata.estimate_request_tokens_rough", lambda *a, **kw: 100)
    await runner._handle_compress_command(_make_event())
    if outcome in {"in_place", "rotation"}:
        runner.session_store.set_session_metadata.assert_called_once_with(entry.session_key, _EXHAUSTION_MARKER, False)
        if outcome == "in_place":
            runner.session_store.rewrite_transcript.assert_not_called()
    else:
        runner.session_store.set_session_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_failed_pause_save_blocks_next_model_turn_in_same_process(tmp_path, monkeypatch):
    import hermes_state
    from gateway.session import SessionStore

    db_type = hermes_state.SessionDB
    monkeypatch.setattr(hermes_state, "SessionDB", lambda: db_type(db_path=tmp_path / "state.db"))
    runner, _ = _make_runner("none")
    event, source = _event_and_source()
    store = SessionStore(tmp_path / "sessions", runner.session_store.config)
    entry = store.get_or_create_session(source)
    runner.session_store.get_or_create_session.side_effect = store.get_or_create_session
    runner.session_store.set_session_metadata.side_effect = store.set_session_metadata
    monkeypatch.setattr(store, "_save", lambda: False)
    runner._run_agent = AsyncMock(return_value=_exhausted_result(AsyncMock(return_value=False)))

    response = await runner._handle_message_with_agent(event, source, entry.session_key, 1)
    assert entry.metadata.get(_EXHAUSTION_MARKER) is not True
    runner._run_agent.assert_awaited_once()
    assert runner._release_turn_lease(entry.session_key, 1)
    runner._run_agent.reset_mock()
    followup, _ = _event_and_source()
    followup.message_id = "post-2"
    next_response = await runner._handle_message_with_agent(followup, source, entry.session_key, 1)

    runner._run_agent.assert_not_awaited()
    assert followup._gateway_skip_goal_continuation is True
    assert "could not be persisted" in response
    assert "after a restart" in response
    assert "could not be persisted" in next_response


@pytest.mark.parametrize("recovery", ["compress", "new", "resume"])
def test_failed_pause_remains_until_recovery_succeeds(tmp_path, monkeypatch, recovery):
    import hermes_state
    from gateway.session import SessionStore

    db_type = hermes_state.SessionDB
    monkeypatch.setattr(hermes_state, "SessionDB", lambda: db_type(db_path=tmp_path / "state.db"))
    cfg = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
    store = SessionStore(tmp_path / "sessions", cfg)
    _, source = _event_and_source()
    entry = store.get_or_create_session(source)
    save = store._save
    monkeypatch.setattr(store, "_save", lambda: False)

    assert store.set_session_metadata(entry.session_key, _EXHAUSTION_MARKER, True) is False
    assert getattr(entry, "compression_paused", False) is True
    assert entry.metadata.get(_EXHAUSTION_MARKER) is not True
    assert "_compression_pause_pending" not in entry.to_dict()
    assert store.set_session_metadata(entry.session_key, _EXHAUSTION_MARKER, False) is False
    assert entry.compression_paused is True

    monkeypatch.setattr(store, "_save", save)
    if recovery == "compress":
        assert store.set_session_metadata(entry.session_key, _EXHAUSTION_MARKER, False)
        recovered = store.get_or_create_session(source)
    elif recovery == "new":
        recovered = store.reset_session(entry.session_key)
    else:
        recovered = store.switch_session(entry.session_key, "other-session")
    assert recovered.compression_paused is False


@pytest.mark.parametrize("replace_session", [False, True])
def test_pending_pause_survives_same_session_routing_recovery(tmp_path, monkeypatch, replace_session):
    import hermes_state
    from gateway.session import SessionStore

    db = hermes_state.SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(hermes_state, "SessionDB", lambda: db)
    cfg = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
    seed = SessionStore(tmp_path / "sessions", cfg)
    _, source = _event_and_source()
    original = seed.get_or_create_session(source)
    load = db.load_gateway_routing_entries
    replace = db.replace_gateway_routing_entries
    readable = False

    def unavailable_load(**kwargs):
        if not readable:
            raise OSError("routing read unavailable")
        return load(**kwargs)

    monkeypatch.setattr(db, "load_gateway_routing_entries", unavailable_load)
    store = SessionStore(tmp_path / "sessions", cfg)
    entry = store.get_or_create_session(source, touch_activity=False)
    assert store._routing_db_loaded is False
    assert store._routing_fallback_baseline is not None
    assert store._routing_fallback_baseline[entry.session_key] == entry.to_dict()
    expected_id = original.session_id
    if replace_session:
        replacement = seed.reset_session(entry.session_key)
        assert replacement is not None
        expected_id = replacement.session_id

    def failed_write_recovers_reads(*args, **kwargs):
        nonlocal readable
        readable = True
        raise OSError("routing write unavailable")

    monkeypatch.setattr(db, "replace_gateway_routing_entries", failed_write_recovers_reads)
    assert store.set_session_metadata(entry.session_key, _EXHAUSTION_MARKER, True) is False
    recovered = store.get_or_create_session(source, touch_activity=False)
    assert store._routing_db_loaded is True
    assert recovered.session_id == expected_id
    assert recovered.compression_paused is (not replace_session)
    assert recovered.metadata.get(_EXHAUSTION_MARKER) is not True

    monkeypatch.setattr(db, "replace_gateway_routing_entries", replace)
    assert store.set_session_metadata(entry.session_key, _EXHAUSTION_MARKER, False)
    assert recovered.compression_paused is False


def test_exhaustion_marker_survives_real_store_reload_until_new(tmp_path, monkeypatch):
    import hermes_state
    from gateway.session import SessionStore
    db_type = hermes_state.SessionDB
    monkeypatch.setattr(hermes_state, "SessionDB", lambda: db_type(db_path=tmp_path / "state.db"))
    cfg = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
    store = SessionStore(tmp_path / "sessions", cfg)
    _, source = _event_and_source()
    entry = store.get_or_create_session(source)
    store.set_session_metadata(entry.session_key, _EXHAUSTION_MARKER, True)
    reopened = SessionStore(tmp_path / "sessions", cfg)
    assert reopened.get_or_create_session(source).metadata[_EXHAUSTION_MARKER] is True
    fresh = reopened.reset_session(entry.session_key)
    assert fresh.session_id != entry.session_id
    assert not SessionStore(tmp_path / "sessions", cfg).get_or_create_session(source).metadata.get(_EXHAUSTION_MARKER)
