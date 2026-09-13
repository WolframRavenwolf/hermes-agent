"""Synthetic native DB/store checks for Cron reverse-context ownership."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cron.context import load_context, save_context
from cron.scheduler import _maybe_mirror_cron_delivery
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


@pytest.fixture
def identity_env(tmp_path, monkeypatch):
    import cron.jobs as jobs
    from hermes_state import SessionDB
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(jobs, "OUTPUT_DIR", home / "cron" / "output")
    config = GatewayConfig()
    config.group_sessions_per_user = True
    config.thread_sessions_per_user = True
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    path = jobs.OUTPUT_DIR / "abcdef123456" / "run.md"
    path.parent.mkdir(parents=True)
    path.write_text("log")
    save_context(path, "Public report", True)
    db = SessionDB()
    store = SessionStore(home / "sessions", config)
    source = SessionSource(platform=Platform.SLACK, chat_id="room", chat_type="group",
                           user_id="alice", thread_id="root", scope_id="team")
    state = SimpleNamespace(db=db, store=store, source=source, path=path, config=config)

    def register():
        src = state.source
        job = {"id": "abcdef123456", "name": "Brief", "_context_file": str(path),
               "origin": src.to_dict()}
        _maybe_mirror_cron_delivery(job, src.platform.value, src.chat_id, "Public report",
                                   thread_id=src.thread_id, user_id=src.user_id, enabled=True)

    state.register = register
    yield state
    db.close()


def create_row(s, source=None, key=None, **fields):
    src = source or s.source
    row = dict(user_id=src.user_id, session_key=key or s.store._generate_session_key(src),
               chat_id=src.chat_id, chat_type=src.chat_type, thread_id=src.thread_id,
               origin_json=json.dumps(src.to_dict()))
    row.update(fields)
    s.db.create_session("selected", source=src.platform.value, **row)
    return "selected"


@pytest.mark.parametrize("damage", ["participant", "platform", "chat", "type", "thread", "scope", "agent", "ended", "private-unowned"])
def test_registration_rejects_wrong_identity_despite_matching_lookup(identity_env, monkeypatch, damage):
    s = identity_env
    changes = {"participant": {"user_id": "bob"}, "platform": {"platform": Platform.TELEGRAM},
               "chat": {"chat_id": "other"}, "type": {"chat_type": "dm"},
               "thread": {"thread_id": "other"}, "scope": {"scope_id": "other"},
               "agent": {"profile": "other"}, "ended": {},
               "private-unowned": {"chat_type": "dm", "chat_id": ""}}[damage]
    wrong = replace(s.source, **changes)
    create_row(s, wrong, key=s.store._generate_session_key(s.source))
    if damage == "ended":
        s.db.end_session("selected", "session_reset")
    monkeypatch.setattr("gateway.mirror._find_session_id", lambda *a, **kw: "selected")
    s.register()
    s.db.append_message("selected", "user", "WRONG_IDENTITY_REPLY")
    assert "WRONG_IDENTITY_REPLY" not in load_context("abcdef123456")
    assert json.loads(s.path.with_suffix(".context.json").read_text())["conversations"] == []


def test_real_sole_wrong_participant_finder_cannot_grant_discussion(identity_env):
    s = identity_env
    create_row(s, replace(s.source, user_id="bob"))
    from gateway.mirror import _find_session_id
    assert _find_session_id("slack", "room", thread_id="root", user_id="alice") == "selected"
    s.register()
    s.db.append_message("selected", "user", "BOB_ONLY_REPLY")
    assert "BOB_ONLY_REPLY" not in load_context("abcdef123456")


@pytest.mark.parametrize("legacy", [False, True])
def test_typed_private_legacy_route_remains_readable(identity_env, legacy):
    s = identity_env
    s.source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", user_id="12345")
    create_row(s, key="agent:main:telegram:dm" if legacy else None)
    s.register()
    s.db.append_message("selected", "user", "SAME_PRIVATE_REPLY")
    assert "SAME_PRIVATE_REPLY" in load_context("abcdef123456")


@pytest.mark.parametrize("damage", ["participant", "chat", "thread", "agent", "scope"])
def test_loading_revalidates_registered_identity(identity_env, damage):
    s = identity_env
    create_row(s)
    s.register()
    s.db.append_message("selected", "user", "BEFORE_ROUTE_MOVE")
    assert "BEFORE_ROUTE_MOVE" in load_context("abcdef123456")
    wrong = replace(s.source, **{"participant": {"user_id": "bob"}, "chat": {"chat_id": "other"},
                                "thread": {"thread_id": "other"}, "agent": {"profile": "other"},
                                "scope": {"scope_id": "other"}}[damage])
    s.db.record_gateway_session_peer("selected", source=wrong.platform.value, user_id=wrong.user_id,
        session_key=s.store._generate_session_key(s.source), chat_id=wrong.chat_id,
        chat_type=wrong.chat_type, thread_id=wrong.thread_id, origin_json=json.dumps(wrong.to_dict()))
    assert "BEFORE_ROUTE_MOVE" not in load_context("abcdef123456")


def test_valid_shared_thread_does_not_require_creator_participant(identity_env):
    s = identity_env
    s.config.thread_sessions_per_user = False
    create_row(s, replace(s.source, user_id="bob"))
    s.register()
    s.db.append_message("selected", "user", "SHARED_REPLY")
    assert "SHARED_REPLY" in load_context("abcdef123456")


def test_registration_accepts_authoritative_typed_origin_when_peer_columns_are_legacy(identity_env, monkeypatch):
    s = identity_env
    s.source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", user_id="12345")
    create_row(s, key="agent:main:telegram:dm", chat_id=None, chat_type=None, thread_id=None)
    monkeypatch.setattr("gateway.mirror._find_session_id", lambda *a, **kw: "selected")
    s.register()
    s.db.append_message("selected", "user", "TYPED_PRIVATE_REPLY")
    assert "TYPED_PRIVATE_REPLY" in load_context("abcdef123456")


def test_originless_private_target_uses_verified_typed_chat_type(identity_env, monkeypatch):
    s = identity_env
    s.source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", user_id="12345")
    create_row(s, key="agent:main:telegram:dm", chat_type=None)
    monkeypatch.setattr("gateway.mirror._find_session_id", lambda *a, **kw: "selected")
    job = {"id": "abcdef123456", "name": "Brief", "_context_file": str(s.path)}
    _maybe_mirror_cron_delivery(job, "telegram", "12345", "Public report", enabled=True)
    s.db.append_message("selected", "user", "TYPED_PRIVATE_REPLY")
    assert "TYPED_PRIVATE_REPLY" in load_context("abcdef123456")


@pytest.mark.parametrize("legacy_key", [False, True])
@pytest.mark.parametrize("canonical_target", [False, True])
def test_private_native_whatsapp_aliases_are_canonicalized_on_both_sides(identity_env, monkeypatch, legacy_key, canonical_target):
    s = identity_env
    raw = "15551234567@s.whatsapp.net"
    s.source = SessionSource(platform=Platform.WHATSAPP, chat_id=raw, user_id=raw, chat_type="dm")
    create_row(s, key=f"agent:main:whatsapp:dm:{raw}" if legacy_key else None)
    if canonical_target:
        from gateway.whatsapp_identity import canonical_whatsapp_identifier
        s.source = replace(s.source, chat_id=canonical_whatsapp_identifier(raw))
    monkeypatch.setattr("gateway.mirror._find_session_id", lambda *a, **kw: "selected")
    s.register()
    s.db.append_message("selected", "user", "NATIVE_ALIAS_REPLY")
    assert "NATIVE_ALIAS_REPLY" in load_context("abcdef123456")
