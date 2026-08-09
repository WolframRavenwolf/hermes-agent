"""Tests for /resume gateway slash command.

Tests the _handle_resume_command handler (switch to a previously-named session)
across gateway messenger platforms.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, build_session_key


def _make_event(text="/resume", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890"):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_matrix_event(
    text="/resume",
    *,
    user_id="@alice:example.org",
    chat_id="!room-a:example.org",
    thread_id=None,
):
    event = _make_event(
        text=text,
        platform=Platform.MATRIX,
        user_id=user_id,
        chat_id=chat_id,
    )
    event.source.chat_type = "group"
    event.source.chat_name = "Current Matrix Room"
    event.source.thread_id = thread_id
    return event


def _record_gateway_origin(
    db,
    session_id,
    source,
    *,
    group_sessions_per_user=True,
    thread_sessions_per_user=False,
):
    """Persist the trusted full gateway provenance written by SessionStore."""
    db.record_gateway_session_peer(
        session_id,
        source=source.platform.value,
        user_id=source.user_id,
        session_key=build_session_key(
            source,
            group_sessions_per_user=group_sessions_per_user,
            thread_sessions_per_user=thread_sessions_per_user,
            profile=source.profile,
        ),
        chat_id=source.chat_id,
        chat_type=source.chat_type,
        thread_id=source.thread_id,
        display_name=source.chat_name,
        origin_json=json.dumps(source.to_dict()),
    )


def _record_matrix_origin(db, session_id, source):
    _record_gateway_origin(db, session_id, source)


def _session_key_for_event(event):
    """Get the session key that build_session_key produces for an event."""
    return build_session_key(event.source)


def _make_runner(
    session_db=None,
    current_session_id="current_session_001",
    event=None,
    *,
    persist_event_origin=True,
):
    """Create a bare GatewayRunner with a mock session store and optional DB."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = SimpleNamespace(platforms={})
    runner._voice_mode = {}
    # Gateway holds the async facade; the slash handlers await it. Tests may
    # explicitly mirror SessionStore persistence for completely blank legacy
    # fixture rows, but never overwrite existing or malformed provenance.
    if session_db is not None:
        if event is not None and persist_event_origin:
            rows = session_db._conn.execute(
                "SELECT id, source, user_id, chat_id, chat_type, thread_id, "
                "session_key, origin_json FROM sessions"
            ).fetchall()
            for row in rows:
                if row["origin_json"] is not None:
                    continue
                if (
                    str(row["source"] or "") == event.source.platform.value
                    and str(row["user_id"] or "") == str(event.source.user_id or "")
                    and str(row["chat_id"] or "") == str(event.source.chat_id or "")
                ):
                    persisted_source = SessionSource.from_dict(event.source.to_dict())
                    if row["chat_type"]:
                        persisted_source.chat_type = row["chat_type"]
                    if row["thread_id"]:
                        persisted_source.thread_id = row["thread_id"]
                    _record_gateway_origin(session_db, row["id"], persisted_source)
        from hermes_state import AsyncSessionDB
        session_db = AsyncSessionDB(session_db)
    runner._session_db = session_db
    runner._running_agents = {}
    runner._is_user_authorized = lambda _source: True

    # Compute the real session key if an event is provided
    session_key = build_session_key(event.source) if event else "agent:main:telegram:dm"

    # Mock session_store that returns a session entry with a known session_id
    mock_session_entry = MagicMock()
    mock_session_entry.session_id = current_session_id
    mock_session_entry.session_key = session_key
    mock_store = MagicMock()
    mock_store.get_or_create_session.return_value = mock_session_entry
    mock_store.load_transcript.return_value = []
    mock_store.switch_session.return_value = mock_session_entry
    runner.session_store = mock_store

    return runner


# ---------------------------------------------------------------------------
# _handle_resume_command
# ---------------------------------------------------------------------------


class TestHandleResumeCommand:
    """Tests for GatewayRunner._handle_resume_command."""

    @pytest.mark.asyncio
    async def test_no_session_db(self):
        """Returns error when session database is unavailable."""
        runner = _make_runner(session_db=None)
        event = _make_event(text="/resume My Project")
        result = await runner._handle_resume_command(event)
        assert "not available" in result.lower()

    @pytest.mark.asyncio
    async def test_list_named_sessions_when_no_arg(self, tmp_path):
        """With no argument, lists recently titled sessions."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        event = _make_event(text="/resume")
        lane_key = _session_key_for_event(event)
        db.create_session(
            "sess_001", "telegram", session_key=lane_key,
            user_id="12345", chat_id="67890",
        )
        db.create_session(
            "sess_002", "telegram", session_key=lane_key,
            user_id="12345", chat_id="67890",
        )
        db.set_session_title("sess_001", "Research")
        db.set_session_title("sess_002", "Coding")

        runner = _make_runner(session_db=db, event=event)
        result = await runner._handle_resume_command(event)
        assert "Research" in result
        assert "Coding" in result
        assert "Named Sessions" in result
        assert "1." in result
        assert "2." in result
        assert "/resume 1" in result
        db.close()


    @pytest.mark.asyncio
    async def test_resume_clears_session_model_overrides(self, tmp_path):
        """Resume must not carry a previous session's /model override into the
        restored conversation, while leaving other chats' overrides intact (#10702)."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("old_session_abc", "telegram", user_id="12345", chat_id="67890")
        db.set_session_title("old_session_abc", "My Project")
        db.create_session("current_session_001", "telegram", user_id="12345", chat_id="67890")

        event = _make_event(text="/resume My Project")
        runner = _make_runner(session_db=db, current_session_id="current_session_001",
                              event=event)
        key = _session_key_for_event(event)
        runner._session_model_overrides = {
            key: {"model": "gpt-5", "provider": "openai"},
            "agent:main:telegram:dm:other": {"model": "keep-me"},
        }
        runner._pending_model_notes = {
            key: "[Note: switched to gpt-5]",
            "agent:main:telegram:dm:other": "[Note: keep-me]",
        }

        result = await runner._handle_resume_command(event)

        assert "Resumed" in result
        # The resumed chat's override + pending note are cleared...
        assert key not in runner._session_model_overrides
        assert key not in runner._pending_model_notes
        # ...but an unrelated chat's state is untouched.
        assert runner._session_model_overrides["agent:main:telegram:dm:other"] == {"model": "keep-me"}
        assert runner._pending_model_notes["agent:main:telegram:dm:other"] == "[Note: keep-me]"
        db.close()

    @pytest.mark.asyncio
    async def test_resume_clears_last_resolved_model(self, tmp_path):
        """Resume must also clear the resumed chat's cached last-resolved
        model, so the restored conversation re-resolves from current config
        instead of a value cached before the switch (mirrors /new and the
        compression-exhausted auto-reset, #58403), while leaving other
        chats' cache entries intact."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("old_session_abc", "telegram", user_id="12345", chat_id="67890")
        db.set_session_title("old_session_abc", "My Project")
        db.create_session("current_session_001", "telegram", user_id="12345", chat_id="67890")

        event = _make_event(text="/resume My Project")
        runner = _make_runner(session_db=db, current_session_id="current_session_001",
                              event=event)
        key = _session_key_for_event(event)
        runner._last_resolved_model = {
            key: "gpt-5",
            "agent:main:telegram:dm:other": "keep-me",
        }

        result = await runner._handle_resume_command(event)

        assert "Resumed" in result
        assert key not in runner._last_resolved_model
        assert runner._last_resolved_model["agent:main:telegram:dm:other"] == "keep-me"
        db.close()


    @pytest.mark.asyncio
    async def test_resume_follows_compression_continuation(self, tmp_path):
        """Gateway /resume should reopen the live descendant after compression."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("compressed_root", "telegram", user_id="12345", chat_id="67890")
        db.set_session_title("compressed_root", "Compressed Work")
        db.end_session("compressed_root", "compression")
        db.create_session("compressed_child", "telegram", user_id="12345", chat_id="67890", parent_session_id="compressed_root")
        db.append_message("compressed_child", "user", "hello from continuation")
        db.create_session("current_session_001", "telegram", user_id="12345", chat_id="67890")

        event = _make_event(text="/resume Compressed Work")
        runner = _make_runner(
            session_db=db,
            current_session_id="current_session_001",
            event=event,
        )
        runner.session_store.load_transcript.side_effect = (
            lambda session_id: [{"role": "user", "content": "hello from continuation"}]
            if session_id == "compressed_child"
            else []
        )

        result = await runner._handle_resume_command(event)

        assert "Resumed session" in result
        assert "(1 message)" in result
        call_args = runner.session_store.switch_session.call_args
        assert call_args[0][1] == "compressed_child"
        runner.session_store.load_transcript.assert_called_with("compressed_child")
        db.close()

    @pytest.mark.asyncio
    async def test_exact_title_skips_foreign_duplicate_before_authorized_candidate(
        self, tmp_path
    ):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        title = "Shared Historical Title"
        db.create_session(
            "foreign_exact",
            "telegram",
            user_id="foreign-user",
            chat_id="foreign-chat",
        )
        db.set_session_title("foreign_exact", title)
        db.create_session(
            "owned_exact",
            "telegram",
            user_id="12345",
            chat_id="67890",
        )
        # Historical databases can contain duplicate exact titles even though
        # the current setter rejects creating them. Reproduce that legacy row
        # shape directly so authorization must inspect every exact candidate.
        assert db._conn is not None
        db._conn.execute("DROP INDEX idx_sessions_title_unique")
        db._conn.execute(
            "UPDATE sessions SET title = ?, started_at = ? WHERE id = ?",
            (title, 200, "owned_exact"),
        )
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (100, "foreign_exact"),
        )
        db._conn.commit()
        db.create_session(
            "current_session_001",
            "telegram",
            user_id="12345",
            chat_id="67890",
        )

        event = _make_event(text=f"/resume {title}")
        runner = _make_runner(
            session_db=db,
            current_session_id="current_session_001",
            event=event,
        )

        result = await runner._handle_resume_command(event)

        assert "Resumed" in result
        switch_call = getattr(runner.session_store.switch_session, "call_args")
        assert switch_call is not None
        assert switch_call[0][1] == "owned_exact"
        db.close()


    @pytest.mark.asyncio
    async def test_resume_evicts_cached_agent(self, tmp_path):
        """Gateway /resume evicts the cached AIAgent so the next message
        rebuilds with the correct session_id end-to-end — mirrors /branch
        and /reset. Without this, the cached agent's memory provider keeps
        writing into the wrong session. See #6672.
        """
        import threading
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("old_session", "telegram", user_id="12345", chat_id="67890")
        db.set_session_title("old_session", "Old Work")
        db.create_session("current_session_001", "telegram", user_id="12345", chat_id="67890")

        event = _make_event(text="/resume Old Work")
        runner = _make_runner(session_db=db, current_session_id="current_session_001",
                              event=event)
        # Seed the cache with a fake agent
        real_key = _session_key_for_event(event)
        runner._agent_cache = {real_key: (MagicMock(), object())}
        runner._agent_cache_lock = threading.RLock()

        await runner._handle_resume_command(event)

        assert real_key not in runner._agent_cache
        db.close()


    @pytest.mark.asyncio
    async def test_bare_resume_lists_exact_lane_before_limit(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        event = _make_event(text="/resume")
        lane_key = _session_key_for_event(event)
        for i in range(3):
            sid = f"lane_{i}"
            db.create_session(
                sid, "telegram", session_key=lane_key,
                user_id="12345", chat_id="67890",
            )
            db.set_session_title(sid, f"Lane Work {i}")
        for i in range(12):
            sid = f"foreign_{i}"
            db.create_session(
                sid, "telegram",
                session_key=f"agent:main:telegram:dm:foreign-{i}",
                user_id=f"foreign-user-{i}", chat_id=f"foreign-{i}",
            )
            db.set_session_title(sid, f"Foreign Work {i}")

        runner = _make_runner(session_db=db, event=event)
        result = await runner._handle_resume_command(event)

        assert "Lane Work 0" in result
        assert "Lane Work 1" in result
        assert "Lane Work 2" in result
        assert "Foreign Work" not in result
        db.close()

    @pytest.mark.asyncio
    async def test_bare_resume_admin_all_preserves_same_platform_widening(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        event = _make_event(text="/resume --all")
        db.create_session(
            "other_lane", "telegram",
            session_key="agent:main:telegram:dm:other",
            user_id="other-user", chat_id="other",
        )
        db.set_session_title("other_lane", "Other Lane Work")

        runner = _make_runner(session_db=db, event=event)
        runner._resume_caller_is_admin = lambda _source: True
        result = await runner._handle_resume_command(event)

        assert "Other Lane Work" in result
        db.close()

    @pytest.mark.asyncio
    async def test_numeric_resume_fallback_uses_exact_lane_candidates(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        event = _make_event(text="/resume 2")
        lane_key = _session_key_for_event(event)
        db.create_session(
            "lane_older", "telegram", session_key=lane_key,
            user_id="12345", chat_id="67890",
        )
        db.set_session_title("lane_older", "Lane Older")
        db.create_session(
            "lane_newer", "telegram", session_key=lane_key,
            user_id="12345", chat_id="67890",
        )
        db.set_session_title("lane_newer", "Lane Newer")
        for i in range(12):
            sid = f"foreign_{i}"
            db.create_session(
                sid, "telegram",
                session_key=f"agent:main:telegram:dm:foreign-{i}",
                user_id=f"foreign-user-{i}", chat_id=f"foreign-{i}",
            )
            db.set_session_title(sid, f"Foreign Work {i}")
        db.create_session(
            "current_session_001", "telegram", session_key=lane_key,
            user_id="12345", chat_id="67890",
        )

        runner = _make_runner(
            session_db=db, current_session_id="current_session_001", event=event
        )
        result = await runner._handle_resume_command(event)

        assert "Resumed" in result
        runner.session_store.switch_session.assert_called_once()
        assert runner.session_store.switch_session.call_args[0][1] == "lane_older"
        db.close()

    @pytest.mark.asyncio
    async def test_bare_resume_normalizes_telegram_lobby_source_to_bound_topic(
        self, tmp_path
    ):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        event = _make_event(text="/resume")
        topic_source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            thread_id="topic-42",
        )
        topic_key = build_session_key(topic_source)
        db.create_session(
            "topic_session", "telegram", session_key=topic_key,
            user_id="12345", chat_id="67890", chat_type="dm",
            thread_id="topic-42",
        )
        db.set_session_title("topic_session", "Recovered Topic Work")
        db.enable_telegram_topic_mode(chat_id="67890", user_id="12345")
        db.bind_telegram_topic(
            chat_id="67890",
            thread_id="topic-42",
            user_id="12345",
            session_key=topic_key,
            session_id="topic_session",
        )
        lobby_key = _session_key_for_event(event)
        db.create_session(
            "lobby_session", "telegram", session_key=lobby_key,
            user_id="12345", chat_id="67890", chat_type="dm",
        )
        db.set_session_title("lobby_session", "Lobby Work")

        runner = _make_runner(session_db=db, event=event)
        result = await runner._handle_resume_command(event)

        assert "Recovered Topic Work" in result
        assert "Lobby Work" not in result
        db.close()

class TestResumeSessionListingAliases:
    """`/resume` flag-only compatibility forms use the secured `/sessions` path."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("resume_text", "sessions_text"),
        [
            ("/resume --all", "/sessions all"),
            ("/resume --full", "/sessions full"),
            ("/resume --all --full", "/sessions all full"),
            ("/resume --full --all", "/sessions all full"),
        ],
    )
    async def test_pure_listing_flags_delegate_to_sessions(
        self, resume_text, sessions_text
    ):
        event = _make_event(text=resume_text)
        runner = _make_runner(event=event)
        runner._session_db = AsyncMock()
        runner._telegram_topic_mode_enabled = lambda source: False
        runner._handle_sessions_command = AsyncMock(return_value="sessions output")

        result = await runner._handle_resume_command(event)

        assert result == "sessions output"
        runner._handle_sessions_command.assert_awaited_once()
        delegated_call = runner._handle_sessions_command.await_args
        assert delegated_call is not None
        delegated = delegated_call.args[0]
        assert delegated is not event
        assert delegated.source is event.source
        assert delegated.text == sessions_text
        assert event.text == resume_text

    @pytest.mark.asyncio
    async def test_full_lists_same_origin_unnamed_without_leaking_foreign_rows(
        self, tmp_path
    ):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            "own_unnamed", "telegram", user_id="12345", chat_id="67890"
        )
        db.append_message("own_unnamed", "user", "own private preview")
        db.create_session(
            "foreign_unnamed", "telegram", user_id="victim", chat_id="other-chat"
        )
        db.append_message("foreign_unnamed", "user", "victim secret preview")

        event = _make_event(text="/resume --full")
        runner = _make_runner(session_db=db, event=event)

        result = await runner._handle_resume_command(event)

        assert "📋 **Sessions**" in result
        assert "own_unnamed" in result
        assert "own private preview" in result
        assert "foreign_unnamed" not in result
        assert "victim secret preview" not in result
        db.close()

    @pytest.mark.asyncio
    async def test_explicit_admin_all_full_uses_sessions_formatter_cross_platform(
        self, tmp_path
    ):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            "tg_named", "telegram", user_id="12345", chat_id="67890"
        )
        db.set_session_title("tg_named", "Admin Telegram Work")
        db.append_message("tg_named", "user", "telegram admin preview")
        db.create_session(
            "discord_unnamed", "discord", user_id="discord-user", chat_id="discord-chat"
        )
        db.append_message("discord_unnamed", "user", "discord unnamed preview")

        event = _make_event(text="/resume --all --full")
        runner = _make_runner(session_db=db, event=event)
        runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
            extra={
                "allow_admin_from": ["12345"],
                "group_allow_admin_from": ["12345"],
            }
        )

        assert runner._resume_caller_is_admin(event.source) is True
        runner._resume_target_allowed = AsyncMock(
            wraps=runner._resume_target_allowed
        )
        result = await runner._handle_resume_command(event)

        assert runner._resume_target_allowed.await_count == 2
        assert all(
            call.kwargs.get("allow_override") is True
            for call in runner._resume_target_allowed.await_args_list
        )
        assert "📋 **Sessions**" in result
        assert "**Admin Telegram Work** `telegram` — `tg_named`" in result
        assert "**—** `discord` — `discord_unnamed`" in result
        assert "telegram admin preview" in result
        assert "discord unnamed preview" in result
        assert "More: `/sessions all`" in result
        db.close()

    @pytest.mark.asyncio
    async def test_non_admin_all_full_cannot_see_foreign_session_metadata(
        self, tmp_path
    ):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            "own_named", "telegram", user_id="12345", chat_id="67890"
        )
        db.set_session_title("own_named", "Own Work")
        db.append_message("own_named", "user", "own visible preview")
        db.create_session(
            "foreign_named", "discord", user_id="victim", chat_id="foreign-chat"
        )
        db.set_session_title("foreign_named", "Victim Secret Title")
        db.append_message("foreign_named", "user", "victim secret preview")

        event = _make_event(text="/resume --all --full")
        runner = _make_runner(session_db=db, event=event)
        runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
            extra={
                "allow_admin_from": ["operator"],
                "group_allow_admin_from": ["operator"],
            }
        )

        assert runner._resume_caller_is_admin(event.source) is False
        result = await runner._handle_resume_command(event)

        assert "own_named" in result
        assert "Own Work" in result
        assert "own visible preview" in result
        assert "foreign_named" not in result
        assert "Victim Secret Title" not in result
        assert "victim secret preview" not in result
        assert "`discord`" not in result
        assert "`telegram`" not in result
        db.close()

    @pytest.mark.asyncio
    async def test_resume_all_without_dashes_remains_a_title(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            "title_all", "telegram", user_id="12345", chat_id="67890"
        )
        db.set_session_title("title_all", "all")
        db.create_session(
            "current_session_001", "telegram", user_id="12345", chat_id="67890"
        )

        event = _make_event(text="/resume all")
        runner = _make_runner(
            session_db=db, current_session_id="current_session_001", event=event
        )
        runner._handle_sessions_command = AsyncMock(return_value="wrong listing path")

        result = await runner._handle_resume_command(event)

        assert "Resumed" in result
        call_args = runner.session_store.switch_session.call_args
        assert call_args[0][1] == "title_all"
        runner._handle_sessions_command.assert_not_awaited()
        db.close()

    @pytest.mark.asyncio
    async def test_all_with_own_target_keeps_direct_authorized_resume_semantics(
        self, tmp_path
    ):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            "own_target", "telegram", user_id="12345", chat_id="67890"
        )
        db.set_session_title("own_target", "Own Target")
        db.create_session(
            "current_session_001", "telegram", user_id="12345", chat_id="67890"
        )

        event = _make_event(text="/resume --all own_target")
        runner = _make_runner(
            session_db=db, current_session_id="current_session_001", event=event
        )
        runner._handle_sessions_command = AsyncMock(return_value="wrong listing path")

        result = await runner._handle_resume_command(event)

        assert "Resumed" in result
        call_args = runner.session_store.switch_session.call_args
        assert call_args[0][1] == "own_target"
        runner._handle_sessions_command.assert_not_awaited()
        db.close()

    @pytest.mark.asyncio
    async def test_unknown_or_mixed_flags_do_not_delegate(self):
        for text in (
            "/resume --unknown",
            "/resume --full own-target",
            "/resume --all --unknown",
        ):
            event = _make_event(text=text)
            runner = _make_runner(event=event)
            runner._session_db = AsyncMock()
            runner._telegram_topic_mode_enabled = lambda source: False
            runner._session_db.get_session.return_value = None
            runner._session_db.resolve_session_by_title.return_value = None
            runner._handle_sessions_command = AsyncMock(return_value="wrong listing path")

            result = await runner._handle_resume_command(event)

            assert "no session found" in result.lower()
            runner._handle_sessions_command.assert_not_awaited()


class TestHandleSessionsCommand:
    """Tests for GatewayRunner._handle_sessions_command."""

    @pytest.mark.asyncio
    async def test_sessions_busy_platform_lists_exact_lane_and_excludes_current_tip(
        self, tmp_path
    ):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        event = _make_event(text="/sessions")
        lane_key = _session_key_for_event(event)
        for i in range(11):
            sid = f"lane_root_{i}"
            db.create_session(
                sid, "telegram", session_key=lane_key,
                user_id="12345", chat_id="67890",
            )
            db.set_session_title(sid, f"Lane Work {i}")

        db.create_session(
            "current_root", "telegram", session_key=lane_key,
            user_id="12345", chat_id="67890",
        )
        db.set_session_title("current_root", "Current compressed root")
        db.end_session("current_root", "compression")
        db.create_session(
            "current_tip", "telegram", session_key=lane_key,
            user_id="12345", chat_id="67890", parent_session_id="current_root",
        )
        db.set_session_title("current_tip", "Current compressed tip")

        for i in range(60):
            sid = f"foreign_{i}"
            db.create_session(
                sid, "telegram",
                session_key=f"agent:main:telegram:dm:foreign-{i}",
                user_id=f"foreign-user-{i}", chat_id=f"foreign-{i}",
            )
            db.set_session_title(sid, f"Foreign Work {i}")

        runner = _make_runner(
            session_db=db, current_session_id="current_tip", event=event
        )
        result = await runner._handle_sessions_command(event)

        assert result.count("Lane Work") == 10
        assert "Lane Work 1" in result
        assert "Lane Work 0" not in result
        assert "Foreign Work" not in result
        assert "current_tip" not in result
        assert "current_root" not in result
        db.close()

    @pytest.mark.asyncio
    async def test_sessions_admin_all_preserves_cross_origin_widening(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        event = _make_event(text="/sessions all")
        lane_key = _session_key_for_event(event)
        db.create_session(
            "tg_named", "telegram", session_key=lane_key,
            user_id="12345", chat_id="67890",
        )
        db.set_session_title("tg_named", "Telegram Work")
        db.create_session(
            "discord_named", "discord",
            session_key="agent:main:discord:dm:other",
            user_id="other-user", chat_id="other",
        )
        db.set_session_title("discord_named", "Discord Work")

        runner = _make_runner(session_db=db, event=event)
        runner._resume_caller_is_admin = lambda _source: True
        result = await runner._handle_sessions_command(event)

        assert "Telegram Work" in result
        assert "Discord Work" in result
        db.close()

    @pytest.mark.asyncio
    async def test_sessions_normalizes_telegram_lobby_source_to_bound_topic(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        event = _make_event(text="/sessions")
        topic_source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            thread_id="topic-42",
        )
        topic_key = build_session_key(topic_source)
        db.create_session(
            "topic_session", "telegram", session_key=topic_key,
            user_id="12345", chat_id="67890", chat_type="dm",
            thread_id="topic-42",
        )
        db.set_session_title("topic_session", "Recovered Topic Work")
        db.enable_telegram_topic_mode(chat_id="67890", user_id="12345")
        db.bind_telegram_topic(
            chat_id="67890",
            thread_id="topic-42",
            user_id="12345",
            session_key=topic_key,
            session_id="topic_session",
        )
        lobby_key = _session_key_for_event(event)
        db.create_session(
            "lobby_session", "telegram", session_key=lobby_key,
            user_id="12345", chat_id="67890", chat_type="dm",
        )
        db.set_session_title("lobby_session", "Lobby Work")

        runner = _make_runner(session_db=db, event=event)
        result = await runner._handle_sessions_command(event)

        assert "Recovered Topic Work" in result
        assert "Lobby Work" not in result
        db.close()



    @pytest.mark.asyncio
    async def test_sessions_all_does_not_leak_cross_origin_for_non_admin(self, tmp_path):
        """`/sessions all` from a non-admin caller must stay scoped to the
        caller's own origin — it must NOT enumerate other origins' sessions
        (the enumeration half of the /resume IDOR). Cross-origin listing is
        gated behind an explicitly-configured admin, which the default test
        config is not."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        event = _make_event(text="/sessions all full")
        lane_key = _session_key_for_event(event)
        db.create_session(
            "tg_named", "telegram", session_key=lane_key,
            user_id="12345", chat_id="67890",
        )
        db.set_session_title("tg_named", "Telegram Work")
        db.create_session("discord_unnamed", "discord")  # other origin
        db.append_message("discord_unnamed", "user", "discord first prompt")

        runner = _make_runner(session_db=db, event=event)

        result = await runner._handle_sessions_command(event)

        # Caller's own (telegram) session is shown; the cross-origin (discord)
        # session is NOT leaked even with `all`.
        assert "Telegram Work" in result
        assert "discord_unnamed" not in result
        assert "Discord" not in result
        db.close()

    @pytest.mark.asyncio
    async def test_sessions_search_finds_older_titled_session(self, tmp_path):
        """`/sessions search <query>` matches titles beyond the recent-10 list
        and orders by activity, keeping the caller's own scope."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        event = _make_event(text="/sessions search an94")
        lane_key = _session_key_for_event(event)
        # Bury the target under newer sessions so a plain listing misses it.
        db.create_session(
            "target_an94", "telegram", session_key=lane_key,
            user_id="12345", chat_id="67890",
        )
        db.set_session_title("target_an94", "AN-94 Prestige Barrel Build #2")
        for i in range(12):
            sid = f"filler_{i}"
            db.create_session(
                sid, "telegram", session_key=lane_key,
                user_id="12345", chat_id="67890",
            )
            db.set_session_title(sid, f"Filler {i}")

        runner = _make_runner(session_db=db, event=event)
        result = await runner._handle_sessions_command(event)

        assert "AN-94 Prestige Barrel Build #2" in result
        assert "target_an94" in result
        assert "Filler" not in result
        db.close()


    @pytest.mark.asyncio
    async def test_sessions_search_does_not_leak_other_users_sessions(self, tmp_path):
        """Search results honor the same owner-scoping guard as listing —
        a matching title owned by a different user/chat must not surface."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        event = _make_event(text="/sessions search an94")
        lane_key = _session_key_for_event(event)
        db.create_session(
            "mine", "telegram", session_key=lane_key,
            user_id="12345", chat_id="67890",
        )
        db.set_session_title("mine", "AN-94 mine")
        db.create_session(
            "theirs", "telegram",
            session_key="agent:main:telegram:dm:55555",
            user_id="99999", chat_id="55555",
        )
        db.set_session_title("theirs", "AN-94 someone else's secret")

        runner = _make_runner(session_db=db, event=event)
        result = await runner._handle_sessions_command(event)

        assert "AN-94 mine" in result
        assert "theirs" not in result
        assert "secret" not in result
        db.close()

    @pytest.mark.asyncio
    async def test_resume_blocks_cross_user_and_unowned_rows(self, tmp_path):
        """An identity-bearing caller cannot resume a session it can't prove it
        owns: a row owned by a different user, or a same-platform row with no
        recorded owner (NULL user_id) must both be denied (IDOR)."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("victim_other_uid", "telegram", user_id="99999")
        db.set_session_title("victim_other_uid", "Other User")
        db.create_session("victim_missing_uid", "telegram")  # NULL owner
        db.set_session_title("victim_missing_uid", "Unowned")
        db.create_session("current_session_001", "telegram", user_id="12345", chat_id="67890")

        for name in ("Other User", "victim_other_uid", "Unowned", "victim_missing_uid"):
            event = _make_event(text=f"/resume {name}")
            runner = _make_runner(session_db=db, current_session_id="current_session_001",
                                  event=event)
            result = await runner._handle_resume_command(event)
            runner.session_store.switch_session.assert_not_called()
            assert "Resumed" not in result, name
        db.close()

    @pytest.mark.asyncio
    async def test_resume_blocks_blank_source_same_uid_row(self, tmp_path):
        """A persisted row whose `source` is blank/legacy cannot prove it shares
        the caller's platform, so user_id equality alone must NOT authorize a
        resume — the blank source fails closed exactly like a missing user_id
        (IDOR regression: an identified caller could otherwise bind to an
        unproven-origin transcript)."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("blank_source_same_uid", "telegram", user_id="12345", chat_id="67890")
        db.set_session_title("blank_source_same_uid", "Blank Source Same UID")
        # Simulate a malformed/legacy row that does not record its origin.
        db._conn.execute(
            "UPDATE sessions SET source = '' WHERE id = ?", ("blank_source_same_uid",)
        )
        db._conn.commit()
        db.create_session("current_session_001", "telegram", user_id="12345", chat_id="67890")

        for name in ("Blank Source Same UID", "blank_source_same_uid"):
            event = _make_event(text=f"/resume {name}")
            runner = _make_runner(session_db=db, current_session_id="current_session_001",
                                  event=event)
            result = await runner._handle_resume_command(event)
            runner.session_store.switch_session.assert_not_called()
            assert "Resumed" not in result, name
        db.close()

    @pytest.mark.asyncio
    async def test_resume_blocks_no_identity_caller_on_persisted_row(self, tmp_path):
        """A caller with no user_id must not resume a persisted row on
        same-platform alone: the row has no chat_id to prove ownership, so a
        Telegram group caller in chat-a (user_id=None) cannot bind to a row
        owned by another chat/user (IDOR regression for the no-identity branch)."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("victim_chat_b_uid", "telegram", user_id="victim")
        db.set_session_title("victim_chat_b_uid", "Victim Chat B")
        db.create_session("current_session_001", "telegram")

        for name in ("Victim Chat B", "victim_chat_b_uid"):
            event = _make_event(text=f"/resume {name}", user_id=None,
                                chat_id="chat-a")
            event.source.chat_type = "group"
            runner = _make_runner(session_db=db, current_session_id="current_session_001",
                                  event=event)
            result = await runner._handle_resume_command(event)
            runner.session_store.switch_session.assert_not_called()
            assert "Resumed" not in result, name
        db.close()

    @pytest.mark.asyncio
    async def test_resume_target_allowed_blocks_no_identity_persisted(self, tmp_path):
        """Unit-level: the persisted-row fallback fails closed for an
        identity-less caller (no live origin resolvable)."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("victim_chat_b_uid", "telegram", user_id="victim")
        runner = _make_runner(session_db=db)
        runner._gateway_session_origin_for_id = lambda session_id: None  # inactive/persisted-only
        caller = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-a",
                               chat_type="group", user_id=None)
        assert await runner._resume_target_allowed(caller, "victim_chat_b_uid",
                                             allow_override=False) is False
        db.close()

    @pytest.mark.asyncio
    async def test_resume_blocks_same_user_different_chat(self, tmp_path):
        """egilewski/CodeRabbit probe: the SAME user must not move a persisted
        transcript from another chat into the current one. The row records its
        records origin chat_id, so a chat-a caller cannot resume a chat-b row even with
        a matching user_id (persisted-row chat-scope proof)."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("same_user_chat_b", "telegram", user_id="12345",
                          chat_id="chat-b")
        db.set_session_title("same_user_chat_b", "Same User Chat B")
        db.create_session("current_session_001", "telegram", user_id="12345",
                          chat_id="chat-a")

        for name in ("Same User Chat B", "same_user_chat_b"):
            event = _make_event(text=f"/resume {name}", user_id="12345",
                                chat_id="chat-a")
            event.source.chat_type = "group"
            runner = _make_runner(session_db=db, current_session_id="current_session_001",
                                  event=event)
            result = await runner._handle_resume_command(event)
            runner.session_store.switch_session.assert_not_called()
            assert "Resumed" not in result, name
        db.close()

    @pytest.mark.asyncio
    async def test_resume_target_allowed_chat_scope(self, tmp_path):
        """Unit-level: identity-bearing persisted fallback requires the row's
        origin chat (and thread) to match the caller's."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("row_chat_a", "telegram", user_id="12345",
                          chat_id="chat-a")
        db.create_session("row_chat_b", "telegram", user_id="12345",
                          chat_id="chat-b")
        db.create_session("row_legacy_nochat", "telegram", user_id="12345")  # NULL chat
        caller = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-a",
                               chat_type="group", user_id="12345")
        _record_gateway_origin(db, "row_chat_a", caller)
        runner = _make_runner(session_db=db)
        runner._gateway_session_origin_for_id = lambda session_id: None  # persisted-only
        # Same chat → allowed; different chat → blocked; legacy NULL-chat → blocked.
        assert await runner._resume_target_allowed(caller, "row_chat_a", allow_override=False) is True
        assert await runner._resume_target_allowed(caller, "row_chat_b", allow_override=False) is False
        assert await runner._resume_target_allowed(caller, "row_legacy_nochat", allow_override=False) is False
        # egilewski/CodeRabbit probe: a GROUP caller that itself has no chat_id
        # must NOT resume a legacy NULL-chat row just because both normalize to
        # "" — a non-DM session is keyed by chat_id, so blank == no provenance.
        blank_caller = SessionSource(platform=Platform.TELEGRAM, chat_id=None,
                                     chat_type="group", user_id="12345")
        assert await runner._resume_target_allowed(blank_caller, "row_legacy_nochat",
                                             allow_override=False) is False
        db.close()

    @pytest.mark.asyncio
    async def test_resume_target_allowed_dm_no_chat_id_scopes_by_user(self, tmp_path):
        """A DM is keyed on user_id; a no-chat_id DM row is resumable by the same
        user (chat_id legitimately absent on both sides), unlike a group row."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            "dm_row", "telegram", user_id="12345", chat_type="dm"
        )  # DM, no chat_id
        same = SessionSource(platform=Platform.TELEGRAM, chat_id=None,
                             chat_type="dm", user_id="12345")
        _record_gateway_origin(db, "dm_row", same)
        runner = _make_runner(session_db=db)
        runner._gateway_session_origin_for_id = lambda session_id: None  # persisted-only
        other = SessionSource(platform=Platform.TELEGRAM, chat_id=None,
                              chat_type="dm", user_id="99999")
        assert await runner._resume_target_allowed(same, "dm_row", allow_override=False) is True
        assert await runner._resume_target_allowed(other, "dm_row", allow_override=False) is False
        db.close()

    @pytest.mark.asyncio
    async def test_resume_target_allowed_shared_group_no_user_match(self, tmp_path):
        """egilewski probe: with group_sessions_per_user=False a non-DM group
        session is shared, so a co-member (different user_id) in the SAME chat
        may resume it — same-chat/thread proof is sufficient, user equality is
        not required. Per-user groups (default) still require the same owner."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("shared_group_row", "telegram", user_id="bob",
                          chat_id="shared-chat", chat_type="group")
        owner = SessionSource(platform=Platform.TELEGRAM, chat_id="shared-chat",
                              chat_type="group", user_id="bob")
        _record_gateway_origin(
            db, "shared_group_row", owner, group_sessions_per_user=False
        )
        runner = _make_runner(session_db=db)
        runner._gateway_session_origin_for_id = lambda session_id: None  # persisted-only
        alice = SessionSource(platform=Platform.TELEGRAM, chat_id="shared-chat",
                              chat_type="group", user_id="alice")

        # Shared group → Alice may resume Bob's row in the same chat.
        runner.config.group_sessions_per_user = False
        assert await runner._resume_target_allowed(alice, "shared_group_row",
                                                   allow_override=False) is True
        # Per-user group → Alice must NOT resume Bob's row (IDOR preserved).
        runner.config.group_sessions_per_user = True
        assert await runner._resume_target_allowed(alice, "shared_group_row",
                                                   allow_override=False) is False
        # A different chat is still blocked even when shared.
        runner.config.group_sessions_per_user = False
        other_chat = SessionSource(platform=Platform.TELEGRAM, chat_id="other-chat",
                                   chat_type="group", user_id="alice")
        assert await runner._resume_target_allowed(other_chat, "shared_group_row",
                                                   allow_override=False) is False
        db.close()


    @pytest.mark.asyncio
    async def test_resume_persisted_fallback_fails_closed_on_user_id_alt(self, tmp_path):
        """egilewski/CodeRabbit probe: Signal/Feishu key the session participant
        on ``user_id_alt or user_id`` (build_session_key), but the sessions table
        stores only user_id. So a persisted per-user row that a caller shares the
        user_id of — but NOT the user_id_alt — maps to a DIFFERENT live session
        key; the persisted fallback must NOT match it on user_id alone (IDOR).

        The live-origin guard already compares user_id_alt correctly; here the
        target is persisted-only, so the fallback fails closed whenever the
        caller keys on user_id_alt and the row can't prove that participant."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        # Persisted rows carry only user_id (no user_id_alt column).
        db.create_session("victim_alt_group", "signal", user_id="+15550001111",
                          chat_id="signal-group", chat_type="group")
        db.create_session("victim_alt_dm", "signal", user_id="+15550001111")  # no chat_id
        runner = _make_runner(session_db=db)
        runner._gateway_session_origin_for_id = lambda session_id: None  # persisted-only

        # Per-user group: attacker shares user_id but has a different user_id_alt
        # → different session key → must fail closed (was: allowed via user_id).
        attacker = SessionSource(platform=Platform.SIGNAL, chat_id="signal-group",
                                 chat_type="group", user_id="+15550001111",
                                 user_id_alt="attacker-uuid")
        assert await runner._resume_target_allowed(attacker, "victim_alt_group",
                                                   allow_override=False) is False
        # No-chat_id DM keyed purely on the participant: same block.
        dm_attacker = SessionSource(platform=Platform.SIGNAL, chat_id=None,
                                    chat_type="dm", user_id="+15550001111",
                                    user_id_alt="attacker-uuid")
        assert await runner._resume_target_allowed(dm_attacker, "victim_alt_dm",
                                                   allow_override=False) is False

        # Regression: a caller WITHOUT user_id_alt (Telegram-style, keyed on
        # user_id) still resumes its own persisted per-user group row.
        tg_db = SessionDB(db_path=tmp_path / "state_tg.db")
        tg_db.create_session("own_group", "telegram", user_id="12345",
                             chat_id="chat-a", chat_type="group")
        tg_caller = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-a",
                                  chat_type="group", user_id="12345")
        _record_gateway_origin(tg_db, "own_group", tg_caller)
        tg_runner = _make_runner(session_db=tg_db)
        tg_runner._gateway_session_origin_for_id = lambda session_id: None
        assert await tg_runner._resume_target_allowed(tg_caller, "own_group",
                                                      allow_override=False) is True

        # Regression: an EXPLICITLY-shared group is unaffected — participant
        # scoping doesn't apply, so an alt-keyed co-member still resumes.
        signal_owner = SessionSource(
            platform=Platform.SIGNAL,
            chat_id="signal-group",
            chat_type="group",
            user_id="+155****1111",
        )
        _record_gateway_origin(
            db,
            "victim_alt_group",
            signal_owner,
            group_sessions_per_user=False,
        )
        runner.config.group_sessions_per_user = False
        assert await runner._resume_target_allowed(attacker, "victim_alt_group",
                                                   allow_override=False) is True
        db.close()
        tg_db.close()

    @pytest.mark.asyncio
    async def test_persisted_alt_identity_allows_exact_canonical_owner(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "signal-alt.db")
        owner = SessionSource(
            platform=Platform.SIGNAL,
            chat_id="signal-group",
            chat_type="group",
            user_id="+155****1111",
            user_id_alt="owner-uuid",
        )
        db.create_session(
            "signal_owner",
            Platform.SIGNAL.value,
            user_id=owner.user_id,
            chat_id=owner.chat_id,
            chat_type=owner.chat_type,
        )
        _record_gateway_origin(db, "signal_owner", owner)
        runner = _make_runner(session_db=db)
        runner._gateway_session_origin_for_id = lambda session_id: None

        assert await runner._resume_target_allowed(
            owner, "signal_owner", allow_override=False
        ) is True
        attacker = SessionSource.from_dict(owner.to_dict())
        attacker.user_id_alt = "attacker-uuid"
        assert await runner._resume_target_allowed(
            attacker, "signal_owner", allow_override=False
        ) is False
        db.close()

    @pytest.mark.asyncio
    async def test_persisted_whatsapp_alias_flip_uses_canonical_key(
        self, tmp_path, monkeypatch
    ):
        from hermes_state import SessionDB

        mapping_dir = tmp_path / "whatsapp" / "session"
        mapping_dir.mkdir(parents=True)
        (mapping_dir / "lid-mapping-999999999999999.json").write_text(
            json.dumps("15551234567@s.whatsapp.net"),
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        db = SessionDB(db_path=tmp_path / "whatsapp-alias.db")
        stored = SessionSource(
            platform=Platform.WHATSAPP,
            chat_id="999999999999999@lid",
            chat_type="dm",
            user_id="999999999999999@lid",
        )
        db.create_session(
            "whatsapp_alias",
            Platform.WHATSAPP.value,
            user_id=stored.user_id,
            chat_id=stored.chat_id,
            chat_type=stored.chat_type,
        )
        _record_gateway_origin(db, "whatsapp_alias", stored)
        caller = SessionSource(
            platform=Platform.WHATSAPP,
            chat_id="15551234567@s.whatsapp.net",
            chat_type="dm",
            user_id="15551234567@s.whatsapp.net",
        )
        runner = _make_runner(session_db=db)
        runner._gateway_session_origin_for_id = lambda session_id: None

        assert build_session_key(stored) == build_session_key(caller)
        assert await runner._resume_target_allowed(
            caller, "whatsapp_alias", allow_override=False
        ) is True
        db.close()

    @pytest.mark.parametrize(
        "missing_key", ["platform", "user_id", "chat_id", "chat_type", "thread_id"]
    )
    def test_persisted_origin_decoder_requires_complete_non_matrix_payload(
        self, missing_key
    ):
        payload = {
            "platform": "telegram",
            "user_id": "12345",
            "chat_id": "67890",
            "chat_type": "group",
            "thread_id": None,
        }
        payload.pop(missing_key)
        runner = _make_runner()
        assert runner._decode_persisted_session_source(payload) is None

    @pytest.mark.asyncio
    async def test_gateway_dispatches_sessions_command(self, tmp_path):
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("tg_session", "telegram", user_id="12345", chat_id="67890")
        db.set_session_title("tg_session", "Telegram Work")

        event = _make_event(text="/sessions")
        runner = _make_runner(session_db=db, event=event)
        runner._handle_sessions_command = AsyncMock(return_value="sessions output")

        result = await runner._handle_message(event)

        assert result == "sessions output"
        runner._handle_sessions_command.assert_awaited_once_with(event)
        db.close()


class TestSameOriginChatGroupScoping:
    """Live group sessions are per-user by default (group_sessions_per_user=True),
    so a co-member must not be able to resume another member's live group session
    via the live-origin branch of _resume_target_allowed (IDOR)."""

    @staticmethod
    def _src(user_id, *, chat_type="group", chat_id="guild-123",
             platform=Platform.DISCORD, user_id_alt=None, thread_id=None):
        return SessionSource(platform=platform, chat_id=chat_id,
                             chat_type=chat_type, user_id=user_id,
                             user_id_alt=user_id_alt, thread_id=thread_id)


    def test_dm_cross_user_blocked_without_chat_id(self):
        # No-chat_id DM: build_session_key falls back to the participant id
        # (user_id_alt or user_id), so two different participants are different
        # origins and must not match. (With a chat_id present the DM key IS the
        # chat_id — see test_dm_same_chat_id_is_same_origin.)
        runner = _make_runner()
        a = self._src("alice", chat_type="dm", chat_id=None)
        b = self._src("bob", chat_type="dm", chat_id=None)
        assert runner._same_origin_chat(a, b) is False


    @pytest.mark.asyncio
    async def test_resume_target_allowed_blocks_cross_user_live_group(self):
        """End-to-end via the live-origin branch: Alice cannot resume Bob's
        active group session in the same chat."""
        runner = _make_runner()
        bob = self._src("bob")
        runner._gateway_session_origin_for_id = lambda sid: bob
        assert await runner._resume_target_allowed(
            self._src("alice"), "bobs_live_sid", allow_override=False
        ) is False

    # --- thread scoping: thread_id is part of the session key, so a session in
    # one thread must never match a caller in another thread of the same chat,
    # even when threads are shared among participants by default. ---


    def test_allows_same_thread_shared_participants(self):
        """Threads are shared by default (thread_sessions_per_user=False), so
        co-members in the SAME thread share the session."""
        runner = _make_runner()
        a = self._src("alice", thread_id="thread-A")
        b = self._src("bob", thread_id="thread-A")
        assert runner._same_origin_chat(a, b) is True


    def test_blocks_thread_vs_no_thread(self):
        """A threaded origin must not match a non-threaded caller in the same
        parent chat (and vice versa)."""
        runner = _make_runner()
        threaded = self._src("alice", thread_id="thread-A")
        parent = self._src("alice", thread_id=None)
        assert runner._same_origin_chat(parent, threaded) is False
        assert runner._same_origin_chat(threaded, parent) is False


class TestResumeRowVisibleMatrixAllScoping:
    """Non-admin Matrix `/resume --all` must NOT enumerate every Matrix titled
    session: the cross-room listing short-circuit is admin-only, mirroring the
    non-Matrix branch. A non-admin `--all` falls back to same-room scoping."""

    @staticmethod
    def _matrix_src(chat_id="!room-a:hs", user_id="@alice:hs"):
        return SessionSource(platform=Platform.MATRIX, chat_id=chat_id,
                             chat_type="group", user_id=user_id)

    @pytest.mark.asyncio
    async def test_non_admin_all_does_not_expose_other_room(self):
        runner = _make_runner()
        runner._resume_caller_is_admin = lambda src: False
        # Titled row whose live origin is a DIFFERENT Matrix room.
        other_room = SessionSource(platform=Platform.MATRIX, chat_id="!room-b:hs",
                                   chat_type="group", user_id="@bob:hs")
        runner._gateway_session_origin_for_id = lambda sid: other_room
        row = {"id": "sid_other_room"}
        assert await runner._resume_row_visible(self._matrix_src(), row, allow_all=True) is False

    @pytest.mark.asyncio
    async def test_non_admin_all_blocks_same_room_other_user_by_default(self):
        runner = _make_runner()
        runner._resume_caller_is_admin = lambda src: False
        same_room_other_user = SessionSource(
            platform=Platform.MATRIX,
            chat_id="!room-a:hs",
            chat_type="group",
            user_id="@bob:hs",
        )
        runner._gateway_session_origin_for_id = lambda sid: same_room_other_user
        row = {"id": "sid_same_room"}
        assert await runner._resume_row_visible(
            self._matrix_src(), row, allow_all=True
        ) is False

    @pytest.mark.asyncio
    async def test_non_admin_all_allows_explicitly_shared_same_room(self):
        runner = _make_runner()
        runner.config.group_sessions_per_user = False
        runner._resume_caller_is_admin = lambda src: False
        same_room_other_user = SessionSource(
            platform=Platform.MATRIX,
            chat_id="!room-a:hs",
            chat_type="group",
            user_id="@bob:hs",
        )
        runner._gateway_session_origin_for_id = lambda sid: same_room_other_user
        row = {"id": "sid_shared_room"}
        assert await runner._resume_row_visible(
            self._matrix_src(), row, allow_all=True
        ) is True

    @pytest.mark.asyncio
    async def test_admin_all_exposes_cross_room(self):
        runner = _make_runner()
        runner._resume_caller_is_admin = lambda src: True
        other_room = SessionSource(platform=Platform.MATRIX, chat_id="!room-b:hs",
                                   chat_type="group", user_id="@bob:hs")
        runner._gateway_session_origin_for_id = lambda sid: other_room
        row = {"id": "sid_other_room"}
        assert await runner._resume_row_visible(self._matrix_src(), row, allow_all=True) is True

    @pytest.mark.asyncio
    async def test_non_admin_all_fails_closed_on_unknown_origin(self):
        runner = _make_runner()
        runner._resume_caller_is_admin = lambda src: False
        runner._gateway_session_origin_for_id = lambda session_id: None
        row = {"id": "sid_unknown"}
        assert await runner._resume_row_visible(self._matrix_src(), row, allow_all=True) is False



class TestSameMatrixRoomThreadScoping:
    """Matrix `/resume` mirrors room/thread and participant-sharing semantics.

    A different thread is always a different session. Room-level sessions are
    per-user by default, while explicitly shared groups and shared threads may be
    resumed by another participant in the same scope.
    """

    @staticmethod
    def _msrc(chat_id="!room-a:hs", user_id="@alice:hs", thread_id=None):
        return SessionSource(platform=Platform.MATRIX, chat_id=chat_id,
                             chat_type="group", user_id=user_id, thread_id=thread_id)

    def test_same_room_no_thread_is_user_scoped_by_default(self):
        runner = _make_runner()
        a = self._msrc(user_id="@alice:hs")
        b = self._msrc(user_id="@bob:hs")
        assert runner._same_matrix_room(a, b) is False

    def test_same_room_no_thread_can_be_explicitly_shared(self):
        runner = _make_runner()
        runner.config.group_sessions_per_user = False
        a = self._msrc(user_id="@alice:hs")
        b = self._msrc(user_id="@bob:hs")
        assert runner._same_matrix_room(a, b) is True


    def test_cross_thread_same_room_blocked(self):
        """The reviewer's probe: caller in thread-a, target origin in thread-b
        of the same room → must not match."""
        runner = _make_runner()
        caller = self._msrc(thread_id="thread-a")
        victim_origin = self._msrc(thread_id="thread-b")
        assert runner._same_matrix_room(caller, victim_origin) is False


    @pytest.mark.asyncio
    async def test_resume_row_visible_blocks_cross_thread(self):
        """End-to-end through the Matrix listing guard."""
        runner = _make_runner()
        runner._resume_caller_is_admin = lambda src: False
        origin_thread_b = self._msrc(thread_id="thread-b")
        runner._gateway_session_origin_for_id = lambda sid: origin_thread_b
        row = {"id": "sid_thread_b"}
        caller_thread_a = self._msrc(thread_id="thread-a")
        assert await runner._resume_row_visible(caller_thread_a, row, allow_all=False) is False


class TestResumeEnumerationAndHistoricalMatrixOrigin:
    """Security and restart/compression regressions for direct/list resume."""

    @staticmethod
    def _create_matrix_session(db, session_id, source, title):
        db.create_session(
            session_id,
            Platform.MATRIX.value,
            user_id=source.user_id,
            chat_id=source.chat_id,
            chat_type=source.chat_type,
            thread_id=source.thread_id,
        )
        db.set_session_title(session_id, title)
        _record_matrix_origin(db, session_id, source)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("target", "set_title"),
        [
            ("foreign_session_id", False),
            ("Guessed Foreign Title", True),
        ],
    )
    async def test_nonmatrix_foreign_and_nonexistent_are_indistinguishable(
        self, tmp_path, target, set_title
    ):
        """A guessed foreign title/id gets exactly the nonexistent response."""
        from hermes_state import SessionDB

        foreign_db = SessionDB(db_path=tmp_path / "foreign.db")
        foreign_db.create_session(
            "foreign_session_id",
            "telegram",
            user_id="victim",
            chat_id="victim-chat",
        )
        if set_title:
            foreign_db.set_session_title("foreign_session_id", target)
        foreign_event = _make_event(text=f"/resume {target}")
        foreign_runner = _make_runner(session_db=foreign_db, event=foreign_event)
        foreign_result = await foreign_runner._handle_resume_command(foreign_event)

        empty_db = SessionDB(db_path=tmp_path / "empty.db")
        missing_event = _make_event(text=f"/resume {target}")
        missing_runner = _make_runner(session_db=empty_db, event=missing_event)
        missing_result = await missing_runner._handle_resume_command(missing_event)

        assert foreign_result == missing_result
        assert "No session found" in foreign_result
        foreign_runner.session_store.switch_session.assert_not_called()
        foreign_runner.session_store.load_transcript.assert_not_called()
        empty_db.close()
        foreign_db.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("target", "resolve_title"),
        [
            ("matrix_foreign_id", False),
            ("Guessed Matrix Title", True),
        ],
    )
    async def test_matrix_foreign_and_nonexistent_are_indistinguishable_without_room_metadata(
        self, target, resolve_title
    ):
        """Neither live Matrix room names nor ids are rendered before auth."""
        caller = _make_matrix_event(text=f"/resume {target}")
        runner = _make_runner(event=caller)
        runner._session_db = AsyncMock()
        foreign_origin = SessionSource(
            platform=Platform.MATRIX,
            chat_id="!classified:example.org",
            chat_name="Classified Foreign Room",
            chat_type="group",
            user_id="@victim:example.org",
        )
        runner._gateway_session_origin_for_id = lambda sid: foreign_origin
        if resolve_title:
            runner._session_db.get_session.return_value = None
            runner._session_db.resolve_session_by_title.return_value = "matrix_foreign_id"
        else:
            runner._session_db.get_session.return_value = {"id": "matrix_foreign_id"}
        runner._session_db.resolve_resume_session_id.return_value = "matrix_foreign_id"

        foreign_result = await runner._handle_resume_command(caller)

        runner._gateway_session_origin_for_id = lambda session_id: None
        runner._session_db.get_session.return_value = None
        runner._session_db.resolve_session_by_title.return_value = None
        nonexistent_result = await runner._handle_resume_command(caller)

        assert foreign_result == nonexistent_result
        assert "No session found" in foreign_result
        assert "Classified Foreign Room" not in foreign_result
        assert "!classified:example.org" not in foreign_result
        runner.session_store.switch_session.assert_not_called()
        runner.session_store.load_transcript.assert_not_called()

    @pytest.mark.asyncio
    async def test_historical_matrix_same_origin_resumes_and_is_visible_in_full_listing(
        self, tmp_path
    ):
        """Persisted exact room/thread/user provenance survives loss of live routing."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        event = _make_matrix_event(
            text="/resume Historical Matrix Work", thread_id="$thread-a"
        )
        self._create_matrix_session(
            db, "historical_matrix", event.source, "Historical Matrix Work"
        )
        db.append_message("historical_matrix", "user", "trusted historical preview")
        db.create_session(
            "current_session_001",
            Platform.MATRIX.value,
            user_id=event.source.user_id,
            chat_id=event.source.chat_id,
            chat_type=event.source.chat_type,
            thread_id=event.source.thread_id,
        )

        runner = _make_runner(
            session_db=db, current_session_id="current_session_001", event=event
        )
        runner._gateway_session_origin_for_id = lambda session_id: None

        direct_result = await runner._handle_resume_command(event)

        assert "Resumed" in direct_result
        assert runner.session_store.switch_session.call_args.args[1] == "historical_matrix"

        listing_event = _make_matrix_event(
            text="/resume --full", thread_id="$thread-a"
        )
        listing_runner = _make_runner(
            session_db=db,
            current_session_id="current_session_001",
            event=listing_event,
        )
        listing_runner._gateway_session_origin_for_id = lambda session_id: None
        listing_runner._resume_target_allowed = AsyncMock(
            wraps=listing_runner._resume_target_allowed
        )

        listing_result = await listing_runner._handle_resume_command(listing_event)

        assert "Historical Matrix Work" in listing_result
        assert "historical_matrix" in listing_result
        assert "trusted historical preview" in listing_result
        listing_runner._resume_target_allowed.assert_awaited()
        db.close()

    @pytest.mark.asyncio
    async def test_historical_matrix_compression_tip_uses_persisted_origin(
        self, tmp_path
    ):
        """A titled compression root resumes its persisted continuation tip."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "compression.db")
        event = _make_matrix_event(
            text="/resume Compressed Matrix Work", thread_id="$thread-a"
        )
        self._create_matrix_session(
            db, "compressed_root", event.source, "Compressed Matrix Work"
        )
        db.end_session("compressed_root", "compression")
        db.create_session(
            "compressed_tip",
            Platform.MATRIX.value,
            user_id=event.source.user_id,
            chat_id=event.source.chat_id,
            chat_type=event.source.chat_type,
            thread_id=event.source.thread_id,
            parent_session_id="compressed_root",
        )
        _record_matrix_origin(db, "compressed_tip", event.source)
        db.append_message("compressed_tip", "user", "continued after compression")
        db.create_session(
            "current_session_001",
            Platform.MATRIX.value,
            user_id=event.source.user_id,
            chat_id=event.source.chat_id,
            chat_type=event.source.chat_type,
            thread_id=event.source.thread_id,
        )

        runner = _make_runner(
            session_db=db, current_session_id="current_session_001", event=event
        )
        runner._gateway_session_origin_for_id = lambda session_id: None
        runner.session_store.load_transcript.side_effect = (
            lambda session_id: [{"role": "user", "content": "continued"}]
            if session_id == "compressed_tip"
            else []
        )

        result = await runner._handle_resume_command(event)

        assert "Resumed" in result
        assert runner.session_store.switch_session.call_args.args[1] == "compressed_tip"
        runner.session_store.load_transcript.assert_called_once_with("compressed_tip")
        db.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("foreign_root", "foreign_tip"),
        [(True, False), (False, True)],
    )
    async def test_compression_requires_authorized_root_and_tip(
        self, tmp_path, foreign_root, foreign_tip
    ):
        """A continuation cannot launder access in either chain direction."""
        from hermes_state import SessionDB

        db = SessionDB(
            db_path=tmp_path / f"chain-{int(foreign_root)}-{int(foreign_tip)}.db"
        )
        event = _make_matrix_event(
            text="/resume Chained Matrix Work", thread_id="$thread-a"
        )
        foreign_source = _make_matrix_event(
            chat_id="!foreign:example.org",
            thread_id="$thread-b",
            user_id="@mallory:example.org",
        ).source
        root_source = foreign_source if foreign_root else event.source
        tip_source = foreign_source if foreign_tip else event.source

        self._create_matrix_session(
            db, "chain_root", root_source, "Chained Matrix Work"
        )
        db.end_session("chain_root", "compression")
        db.create_session(
            "chain_tip",
            Platform.MATRIX.value,
            user_id=tip_source.user_id,
            chat_id=tip_source.chat_id,
            chat_type=tip_source.chat_type,
            thread_id=tip_source.thread_id,
            parent_session_id="chain_root",
        )
        _record_matrix_origin(db, "chain_tip", tip_source)
        db.create_session(
            "current_session_001",
            Platform.MATRIX.value,
            user_id=event.source.user_id,
            chat_id=event.source.chat_id,
            chat_type=event.source.chat_type,
            thread_id=event.source.thread_id,
        )

        runner = _make_runner(
            session_db=db,
            current_session_id="current_session_001",
            event=event,
        )
        runner._gateway_session_origin_for_id = lambda session_id: None

        result = await runner._handle_resume_command(event)

        assert "No session found" in result
        runner.session_store.switch_session.assert_not_called()
        runner.session_store.load_transcript.assert_not_called()

        listing_event = _make_matrix_event(
            text="/resume --full", thread_id="$thread-a"
        )
        listing_runner = _make_runner(
            session_db=db,
            current_session_id="current_session_001",
            event=listing_event,
        )
        listing_runner._gateway_session_origin_for_id = lambda session_id: None
        listing_result = await listing_runner._handle_resume_command(listing_event)
        assert "chain_root" not in listing_result
        assert "chain_tip" not in listing_result
        assert "Chained Matrix Work" not in listing_result
        db.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("caller_chat", "caller_thread", "caller_user"),
        [
            ("!wrong-room:example.org", "$thread-a", "@alice:example.org"),
            ("!room-a:example.org", "$thread-b", "@alice:example.org"),
        ],
    )
    async def test_historical_matrix_wrong_room_or_thread_denied_and_hidden(
        self, tmp_path, caller_chat, caller_thread, caller_user
    ):
        from hermes_state import SessionDB

        db = SessionDB(
            db_path=tmp_path
            / f"{caller_chat.replace('/', '_')}-{caller_thread}-{caller_user}.db"
        )
        owner_event = _make_matrix_event(thread_id="$thread-a")
        owner_event.source.chat_name = "Private Historical Room"
        self._create_matrix_session(
            db, "historical_matrix", owner_event.source, "Private Historical Title"
        )
        db.append_message("historical_matrix", "user", "private historical preview")

        caller = _make_matrix_event(
            text="/resume historical_matrix",
            chat_id=caller_chat,
            thread_id=caller_thread,
            user_id=caller_user,
        )
        runner = _make_runner(session_db=db, event=caller)
        runner._gateway_session_origin_for_id = lambda session_id: None

        direct_result = await runner._handle_resume_command(caller)

        assert "No session found" in direct_result
        assert "Private Historical Room" not in direct_result
        assert "!room-a:example.org" not in direct_result
        runner.session_store.switch_session.assert_not_called()
        runner.session_store.load_transcript.assert_not_called()

        listing_event = _make_matrix_event(
            text="/resume --full",
            chat_id=caller_chat,
            thread_id=caller_thread,
            user_id=caller_user,
        )
        listing_runner = _make_runner(session_db=db, event=listing_event)
        listing_runner._gateway_session_origin_for_id = lambda session_id: None
        listing_result = await listing_runner._handle_resume_command(listing_event)

        assert "historical_matrix" not in listing_result
        assert "Private Historical Title" not in listing_result
        assert "private historical preview" not in listing_result
        db.close()

    @pytest.mark.asyncio
    async def test_resume_and_sessions_paginate_past_newer_foreign_rows(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "foreign-pagination.db")
        own_event = _make_matrix_event(text="/resume", thread_id="$thread-a")
        self._create_matrix_session(
            db, "owned_older", own_event.source, "Owned Older Session"
        )
        for index in range(15):
            foreign_source = _make_matrix_event(
                text="/resume",
                chat_id=f"!foreign-{index}:example.org",
                user_id="@mallory:example.org",
                thread_id="$foreign-thread",
            ).source
            self._create_matrix_session(
                db,
                f"foreign_newer_{index}",
                foreign_source,
                f"Foreign Newer {index}",
            )

        list_runner = _make_runner(
            session_db=db, event=own_event, persist_event_origin=False
        )
        list_runner._gateway_session_origin_for_id = lambda session_id: None
        resume_list = await list_runner._handle_resume_command(own_event)
        assert "Owned Older Session" in resume_list
        assert "Foreign Newer" not in resume_list

        numeric_event = _make_matrix_event(text="/resume 1", thread_id="$thread-a")
        numeric_runner = _make_runner(
            session_db=db, event=numeric_event, persist_event_origin=False
        )
        numeric_runner._gateway_session_origin_for_id = lambda session_id: None
        await numeric_runner._handle_resume_command(numeric_event)
        assert (
            getattr(numeric_runner.session_store.switch_session, "call_args").args[1]
            == "owned_older"
        )

        sessions_event = _make_matrix_event(text="/sessions", thread_id="$thread-a")
        sessions_runner = _make_runner(
            session_db=db, event=sessions_event, persist_event_origin=False
        )
        sessions_runner._gateway_session_origin_for_id = lambda session_id: None
        sessions_result = await sessions_runner._handle_sessions_command(sessions_event)
        assert "Owned Older Session" in sessions_result
        assert "Foreign Newer" not in sessions_result
        db.close()

    @pytest.mark.asyncio
    async def test_title_resolution_skips_newer_foreign_numbered_variant(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "title-shadow.db")
        event = _make_matrix_event(
            text="/resume Shared Title", thread_id="$thread-a"
        )
        self._create_matrix_session(db, "owned_exact", event.source, "Shared Title")
        foreign_source = _make_matrix_event(
            text="/resume",
            chat_id="!foreign:example.org",
            user_id="@mallory:example.org",
            thread_id="$foreign-thread",
        ).source
        self._create_matrix_session(
            db, "foreign_numbered", foreign_source, "Shared Title #2"
        )

        runner = _make_runner(
            session_db=db, event=event, persist_event_origin=False
        )
        runner._gateway_session_origin_for_id = lambda session_id: None
        result = await runner._handle_resume_command(event)

        assert "Shared Title" in result
        assert (
            getattr(runner.session_store.switch_session, "call_args").args[1]
            == "owned_exact"
        )
        db.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_origin",
        [
            None,
            "",
            "{malformed SECRET_ORIGIN",
            json.dumps(
                {
                    "platform": "matrix",
                    "chat_id": "!room-a:example.org",
                    "chat_type": "group",
                    "user_id": "@alice:example.org",
                    # Missing thread_id is incomplete provenance.
                }
            ),
            json.dumps(
                {
                    "platform": "matrix",
                    "chat_id": "!room-a:example.org",
                    "user_id": "@alice:example.org",
                    "thread_id": "$thread-a",
                    # Missing chat_type must not default to DM.
                }
            ),
            json.dumps(
                {
                    "platform": "matrix",
                    "chat_id": "!room-a:example.org",
                    "chat_type": "group",
                    "thread_id": "$thread-a",
                    # Missing user_id is incomplete provenance.
                }
            ),
        ],
    )
    async def test_historical_matrix_missing_or_malformed_origin_fails_closed(
        self, tmp_path, bad_origin
    ):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "malformed.db")
        event = _make_matrix_event(
            text="/resume malformed_history", thread_id="$thread-a"
        )
        db.create_session(
            "malformed_history",
            Platform.MATRIX.value,
            user_id=event.source.user_id,
            chat_id=event.source.chat_id,
            chat_type=event.source.chat_type,
            thread_id=event.source.thread_id,
        )
        _record_gateway_origin(db, "malformed_history", event.source)
        db.set_session_title("malformed_history", "Malformed Historical Title")
        db._conn.execute(
            "UPDATE sessions SET origin_json = ? WHERE id = ?",
            (bad_origin, "malformed_history"),
        )
        db._conn.commit()

        runner = _make_runner(
            session_db=db, event=event, persist_event_origin=False
        )
        runner._gateway_session_origin_for_id = lambda session_id: None
        runner._decode_persisted_session_source = MagicMock(
            wraps=runner._decode_persisted_session_source
        )
        result = await runner._handle_resume_command(event)

        assert "No session found" in result
        assert "SECRET_ORIGIN" not in result
        runner._decode_persisted_session_source.assert_called()
        runner.session_store.switch_session.assert_not_called()
        runner.session_store.load_transcript.assert_not_called()

        listing_event = _make_matrix_event(
            text="/resume --full", thread_id="$thread-a"
        )
        listing_runner = _make_runner(
            session_db=db,
            event=listing_event,
            persist_event_origin=False,
        )
        listing_runner._gateway_session_origin_for_id = lambda session_id: None
        listing_runner._decode_persisted_session_source = MagicMock(
            wraps=listing_runner._decode_persisted_session_source
        )
        listing_result = await listing_runner._handle_resume_command(listing_event)
        assert "malformed_history" not in listing_result
        assert "Malformed Historical Title" not in listing_result
        listing_runner._decode_persisted_session_source.assert_called()
        db.close()

    @pytest.mark.asyncio
    async def test_matrix_row_chat_type_contradiction_is_hidden(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "chat-type-contradiction.db")
        event = _make_matrix_event(
            text="/resume Contradictory Matrix", thread_id="$thread-a"
        )
        self._create_matrix_session(
            db, "contradictory_matrix", event.source, "Contradictory Matrix"
        )
        db.append_message(
            "contradictory_matrix", "user", "private contradictory preview"
        )
        conn = db._conn
        assert conn is not None
        conn.execute(
            "UPDATE sessions SET chat_type = ? WHERE id = ?",
            ("channel", "contradictory_matrix"),
        )
        conn.commit()

        runner = _make_runner(
            session_db=db, event=event, persist_event_origin=False
        )
        runner._gateway_session_origin_for_id = lambda session_id: None
        direct_result = await runner._handle_resume_command(event)
        assert "No session found" in direct_result
        assert "contradictory_matrix" not in direct_result
        assert "private contradictory preview" not in direct_result

        listing_event = _make_matrix_event(
            text="/resume --full", thread_id="$thread-a"
        )
        listing_runner = _make_runner(
            session_db=db,
            event=listing_event,
            persist_event_origin=False,
        )
        listing_runner._gateway_session_origin_for_id = lambda session_id: None
        listing_result = await listing_runner._handle_resume_command(listing_event)
        assert "contradictory_matrix" not in listing_result
        assert "private contradictory preview" not in listing_result
        db.close()

    @pytest.mark.asyncio
    async def test_matrix_admin_explicit_override_retains_historical_access(
        self, tmp_path
    ):
        """An explicit configured-admin cross-room override remains available."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "admin.db")
        caller = _make_matrix_event(
            text="/resume --cross-room admin_target",
            user_id="@admin:example.org",
        )
        db.create_session(
            "admin_target",
            Platform.MATRIX.value,
            user_id="@victim:example.org",
            chat_id="!foreign:example.org",
            chat_type="group",
        )
        db.set_session_title("admin_target", "Admin Target")
        db._conn.execute(
            "UPDATE sessions SET origin_json = ? WHERE id = ?",
            ("{malformed but admin-only", "admin_target"),
        )
        db._conn.commit()

        runner = _make_runner(session_db=db, event=caller)
        runner._gateway_session_origin_for_id = lambda session_id: None
        runner.config.platforms[Platform.MATRIX] = PlatformConfig(
            extra={
                "allow_admin_from": ["@admin:example.org"],
                "group_allow_admin_from": ["@admin:example.org"],
            }
        )

        result = await runner._handle_resume_command(caller)

        assert "Cross-room resume" in result
        assert runner.session_store.switch_session.call_args.args[1] == "admin_target"
        db.close()

