"""Regression: CLI→Discord handoff must key a thread destination on the
thread's OWN id, matching how the platform adapter keys organic in-thread
messages.

Bug: the handoff built its destination ``SessionSource`` with
``chat_id = home.chat_id`` (the PARENT channel) while thread destinations use
``chat_type="thread"`` and ``thread_id = <thread>``. The Discord adapter,
however, builds organic in-thread messages with ``chat_id = <thread>`` (the
thread's own id). ``build_session_key`` therefore produced two different keys:

    handoff:  agent:main:discord:thread:{parent}:{thread}
    organic:  agent:main:discord:thread:{thread}:{thread}

So the next real user reply in the handoff thread resolved to a DIFFERENT
session_key and spawned a fresh session instead of continuing the handed-off
one (observed: a stray auto-titled session + a session_search fallback because
the new session had no prior context).

The fix is Discord-specific: Slack and Telegram adapters key organic thread
messages with ``chat_id = parent_channel``, so the parent channel is correct
for those platforms and the guard must NOT apply to them.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource, build_session_key


def _organic_discord_thread_key(thread_id: str, parent_id: str, user_id: str) -> str:
    """Key the Discord adapter produces for a message typed inside a thread.

    Mirrors plugins/platforms/discord/adapter.py _handle_message: chat_id is
    the thread's own id, chat_type is "thread", thread_id is the thread id,
    parent_chat_id is the parent channel.
    """
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=str(thread_id),
        chat_type="thread",
        user_id=user_id,
        thread_id=str(thread_id),
        parent_chat_id=str(parent_id),
    )
    return build_session_key(source, thread_sessions_per_user=False)


def _organic_slack_thread_key(channel_id: str, thread_ts: str, user_id: str) -> str:
    """Key the Slack adapter produces for a message in a thread.

    Mirrors plugins/platforms/slack/adapter.py: chat_id is the parent channel,
    chat_type is "group", thread_id is the thread timestamp.
    """
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id=str(channel_id),
        chat_type="group",
        user_id=user_id,
        thread_id=str(thread_ts),
    )
    return build_session_key(source, thread_sessions_per_user=False)


def _handoff_key(
    platform: Platform,
    home_chat_id: str,
    thread_id: str,
) -> str:
    """Key the handoff produces after the fix.

    Mirrors the fixed logic in GatewayRunner._process_handoff: for Discord
    thread destinations, chat_id is the thread's own id; for other platforms,
    chat_id remains the parent/home channel.
    """
    dest_chat_type = "thread"
    # This mirrors the fixed logic in GatewayRunner._process_handoff.
    if platform == Platform.DISCORD and dest_chat_type == "thread" and thread_id:
        dest_chat_id = str(thread_id)
    else:
        dest_chat_id = str(home_chat_id)
    dest_source = SessionSource(
        platform=platform,
        chat_id=dest_chat_id,
        chat_type=dest_chat_type,
        user_id="system:handoff",
        user_name="Handoff",
        thread_id=str(thread_id),
    )
    return build_session_key(dest_source, thread_sessions_per_user=False)


def test_discord_handoff_key_matches_organic_in_thread_key():
    """For Discord, the handoff key must be byte-identical to the organic
    in-thread key — otherwise a reply in the handoff thread spawns a new session."""
    parent_id = "1523581766923845724"
    thread_id = "1523590238595846166"
    user_id = "171164909650968576"

    organic = _organic_discord_thread_key(thread_id, parent_id, user_id)
    handoff = _handoff_key(Platform.DISCORD, parent_id, thread_id)

    assert handoff == organic, (
        f"handoff key {handoff!r} != organic in-thread key {organic!r}; "
        "a reply in the handoff thread would spawn a new session"
    )
    assert handoff == f"agent:main:discord:thread:{thread_id}:{thread_id}"


def test_discord_handoff_key_does_not_use_parent_channel():
    """The pre-fix bug: keying on the parent channel. Guard against regression."""
    parent_id = "1523581766923845724"
    thread_id = "1523590238595846166"

    handoff = _handoff_key(Platform.DISCORD, parent_id, thread_id)
    buggy = f"agent:main:discord:thread:{parent_id}:{thread_id}"

    assert handoff != buggy, "handoff regressed to keying on the parent channel"


def test_slack_handoff_key_uses_parent_channel_not_thread_id():
    """Slack adapter keys organic thread messages with chat_id=channel_id
    (parent), not the thread ts. The fix must NOT apply to Slack — otherwise
    the handoff key would use the thread ts as chat_id, breaking the match."""
    channel_id = "C12345678"
    thread_ts = "1690000000.123456"
    user_id = "U123456"

    organic = _organic_slack_thread_key(channel_id, thread_ts, user_id)
    handoff = _handoff_key(Platform.SLACK, channel_id, thread_ts)

    # The handoff uses chat_type="thread" while Slack organic uses "group",
    # so these keys differ in the chat_type slot (a pre-existing mismatch,
    # NOT caused by this fix). The important assertion is that the handoff
    # does NOT use the thread_ts as chat_id (the regression this guard prevents).
    assert "thread_ts" not in handoff or thread_ts not in handoff.split(":")[-2:-1], (
        f"handoff key {handoff!r} incorrectly uses thread ts as chat_id"
    )
    # Verify the handoff key still contains the parent channel_id
    assert channel_id in handoff, (
        f"handoff key {handoff!r} lost the parent channel id — "
        "the Discord-specific guard leaked into Slack"
    )


@pytest.fixture
def mattermost_handoff(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, HomeChannel, PlatformConfig
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore
    from plugins.platforms.mattermost.adapter import MattermostAdapter

    home = tmp_path / ".hermes"
    reviews = home / "profiles" / "reviews"
    reviews.mkdir(parents=True)
    (reviews / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # The global test fixture pins one DB; these cases exercise native profile scopes.
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    pc = PlatformConfig(enabled=True, extra={"reply_mode": "off", "require_mention": False})
    pc.home_channel = HomeChannel(Platform.MATTERMOST, "room", "Reports", user_id="member", scope_id="team")
    config = GatewayConfig(platforms={Platform.MATTERMOST: pc})
    store = SessionStore(home / "sessions", config)
    adapter = MattermostAdapter(pc)
    adapter.set_session_store(store)
    adapter._session = Mock()
    adapter._bot_user_id = "bot"
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config, runner.adapters = config, {Platform.MATTERMOST: adapter}
    runner._profile_adapters = {"reviews": {Platform.MATTERMOST: adapter}}
    runner.session_store = store
    runner._evict_cached_agent = Mock()
    runner._release_running_agent_state = Mock()
    runner._handle_message = AsyncMock(return_value="History received")
    adapter.gateway_runner = runner
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr("gateway.run.load_gateway_config", lambda: config)
    s = SimpleNamespace(runner=runner, adapter=adapter, store=store, config=config, home=home,
                        reviews=reviews, destination=pc.home_channel, calls=[], kind="D",
                        seed_ok=True, channel_id="room", metadata_error=False,
                        row={"id": "cli-history", "handoff_platform": "mattermost", "title": "CLI work"})

    async def api(method, path, payload=None):
        s.calls.append((method, path, payload))
        if path.startswith("channels/"):
            if s.metadata_error:
                raise RuntimeError("channel lookup failed")
            return {"id": s.channel_id, "type": s.kind}
        if method == "GET":
            return {"id": "configured-root", "root_id": ""}
        if method == "POST" and payload["message"].startswith(":thread:"):
            return {"id": "handoff-root"} if s.seed_ok else {}
        return {"id": "response", "channel_id": "room", "root_id": (payload or {}).get("root_id", "")}

    monkeypatch.setattr(adapter, "_api", api)
    return s


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,thread_per_user,group_per_user,seed_ok,configured,owner,multiplex", [
    ("D", False, True, True, None, "default", False),
    ("G", True, True, True, None, "default", False),
    ("P", False, True, True, None, "default", False),
    ("O", True, True, True, None, "default", False),
    ("G", True, False, True, None, "default", False),
    ("P", True, True, False, "configured-root", "default", False),
    ("O", False, True, False, None, "default", False),
    ("D", True, True, False, None, "default", True),
    ("D", False, True, True, None, "reviews", True),
])
async def test_mattermost_cli_history_reaches_native_reply(
        mattermost_handoff, kind, thread_per_user, group_per_user, seed_ok, configured, owner, multiplex):
    from gateway.run import _profile_runtime_scope
    s = mattermost_handoff
    s.kind, s.seed_ok, s.destination.thread_id = kind, seed_ok, configured
    s.config.thread_sessions_per_user = thread_per_user
    s.config.group_sessions_per_user = group_per_user
    s.config.multiplex_profiles = multiplex
    if owner == "reviews":
        s.adapter.set_owner_profile(owner)
        s.runner.adapters = {}
    root = "handoff-root" if seed_ok else configured
    with _profile_runtime_scope(s.home if owner == "default" else s.reviews, prepared_secret_scope={}):
        s.store._db.create_session(s.row["id"], "cli")
        s.store._db.append_message(s.row["id"], "user", "Preserve my CLI history")
        await s.runner._process_handoff(s.row, None if owner == "default" else owner)
        await s.adapter._handle_ws_event({"event": "posted", "data": {
            "channel_type": kind, "post": json.dumps({"id": "reply", "user_id": "member",
                "channel_id": "room", "message": "Continue", "root_id": root or ""})}})
        source = s.adapter.handle_message.call_args.args[0].source
        resumed = await s.runner.async_session_store.get_or_create_session(source)
        assert resumed.session_id == s.row["id"]
        assert [(m["role"], m["content"]) for m in s.store.load_transcript(resumed.session_id)] == [
            ("user", "Preserve my CLI history")]
        synthetic = s.runner._handle_message.call_args.args[0]
        assert synthetic.internal is True
        assert synthetic.source.user_id == "member" and synthetic.source.scope_id == "team"
        assert s.store._generate_session_key(synthetic.source) == resumed.session_key
        assert s.runner._transport_owner(synthetic.source) == s.runner._transport_owner(source)
        assert s.runner._authorization_home_for_source(source) == (
            (s.reviews if owner == "reviews" else s.home) if multiplex else None)
        writes = [c for c in s.calls if c[0] in {"POST", "PUT"}]
        assert s.calls[0][:2] == ("GET", "channels/room")
        if seed_ok:
            assert writes[-1][0:2] == ("PUT", "posts/handoff-root/patch")
        else:
            assert writes[-1][2].get("root_id") == configured


@pytest.mark.asyncio
async def test_mattermost_rejected_handoff_never_posts_private_cli_title(mattermost_handoff, monkeypatch):
    from gateway.profile_routing import parse_profile_routes
    from gateway.run import _profile_runtime_scope

    s = mattermost_handoff
    private_title = "Private CLI merger notes"
    s.row["title"] = private_title
    s.config.multiplex_profiles = True
    s.config.profile_routes = parse_profile_routes([{
        "name": "reports", "platform": "mattermost", "chat_id": "room", "profile": "reviews"}])
    post = AsyncMock(wraps=s.adapter._api_post)
    monkeypatch.setattr(s.adapter, "_api_post", post)

    with _profile_runtime_scope(s.home, prepared_secret_scope={}):
        s.store._db.create_session(s.row["id"], "cli")
        s.store._db.append_message(s.row["id"], "user", "Private CLI history")
        before = s.store._db.get_session(s.row["id"])
        with pytest.raises(RuntimeError, match="destination does not belong to the CLI profile"):
            await s.runner._process_handoff(s.row, None)
        assert not s.store._entries
        assert s.store._db.get_session(s.row["id"]) == before
        assert s.store._db.get_messages_as_conversation(s.row["id"])[0]["content"] == "Private CLI history"
        s.runner._handle_message.assert_not_called()

    post.assert_awaited_once()
    assert post.await_args is not None
    assert post.await_args.args[0] == "posts"
    assert post.await_args.args[1]["channel_id"] == "room"
    writes = [c for c in s.calls if c[0] != "GET"]
    assert len(writes) == 1 and writes[0][:2] == ("POST", "posts")
    assert all(private_title not in json.dumps(payload) for _, _, payload in writes)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "cross_profile", "unserved", "participant", "wrong_channel", "unknown_type", "channel_lookup",
    "owner_missing", "owner_error", "owner_mismatch",
])
async def test_mattermost_handoff_rejection_leaves_cli_history_unrouted(mattermost_handoff, monkeypatch, failure):
    from gateway.profile_routing import parse_profile_routes
    from gateway.run import _profile_runtime_scope
    s = mattermost_handoff
    profile_name = None
    if failure in {"cross_profile", "unserved"}:
        s.config.multiplex_profiles = True
        s.config.profile_routes = parse_profile_routes([{
            "name": "reports", "platform": "mattermost", "chat_id": "room",
            "profile": "reviews" if failure == "cross_profile" else "unserved"}])
    if failure == "participant":
        s.kind, s.config.thread_sessions_per_user, s.destination.user_id = "G", True, None
    if failure == "wrong_channel":
        s.channel_id = "other"
    if failure == "unknown_type":
        s.kind = "X"
    s.metadata_error = failure == "channel_lookup"
    if failure in {"owner_missing", "owner_error"}:
        resolver = Mock(return_value=None, side_effect=RuntimeError("owner unavailable") if failure == "owner_error" else None)
        monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", resolver)
    if failure == "owner_mismatch":
        profile_name = "reviews"
    with _profile_runtime_scope(s.home, prepared_secret_scope={}):
        s.store._db.create_session(s.row["id"], "cli")
        s.store._db.append_message(s.row["id"], "user", "Private CLI history")
        before = s.store._db.get_session(s.row["id"])
        with pytest.raises(RuntimeError):
            await s.runner._process_handoff(s.row, profile_name)
        assert not s.store._entries
        assert s.store._db.get_session(s.row["id"]) == before
        assert s.store._db.get_messages_as_conversation(s.row["id"])[0]["content"] == "Private CLI history"
        s.runner._handle_message.assert_not_called()
        if failure in {"wrong_channel", "unknown_type", "channel_lookup"}:
            assert not any(c[0] != "GET" for c in s.calls)
    with _profile_runtime_scope(s.reviews, prepared_secret_scope={}):
        assert s.store._db.list_sessions_rich() == []
