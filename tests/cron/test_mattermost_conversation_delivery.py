"""Mattermost delivery receipts must seed the exact incoming reply session."""

import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def delivery_env(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.session import SessionStore
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import cron.jobs as jobs
    monkeypatch.setattr(jobs, "OUTPUT_DIR", home / "cron" / "output")
    platform = Platform("mattermost")
    config = GatewayConfig(platforms={platform: PlatformConfig(enabled=True)})
    store = SessionStore(home / "sessions", config)
    from plugins.platforms.mattermost.adapter import MattermostAdapter
    adapter = MattermostAdapter(PlatformConfig(enabled=True, token="fixture", extra={"url": "https://mm.invalid"}))
    adapter._session_store = store
    adapter.supports_inchannel_continuable = False
    adapter.create_handoff_thread = AsyncMock(return_value=None)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    monkeypatch.setattr("cron.scheduler.load_config", lambda: {"cron": {"wrap_response": False}})
    def run(coro, loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:
            future.set_exception(exc)
        return future
    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", run)
    return home, platform, store, adapter


@pytest.mark.parametrize("chat_type", ["group", "channel", "dm"])
def test_real_report_root_seeds_matching_reply_and_records_context(delivery_env, monkeypatch, chat_type):
    from cron.context import save_context, load_context
    from cron.scheduler import _deliver_result
    from gateway.platforms.base import SendResult
    from gateway.session import SessionSource
    home, platform, store, adapter = delivery_env
    path = home / "cron" / "output" / "abcdef123456" / "2026-09-13_12-00-00.md"
    path.parent.mkdir(parents=True)
    path.write_text("log")
    save_context(path, "Report: task two is open.", True)
    send = AsyncMock(return_value=SendResult(success=True, message_id="last-chunk", raw_response={"cron_root_id": "first-post", "cron_chat_type": chat_type}))
    monkeypatch.setattr("gateway.delivery.DeliveryRouter._deliver_to_platform", send)
    job = {"id": "abcdef123456", "name": "Brief", "deliver": "mattermost:target-channel",
           "attach_to_session": True, "_context_file": str(path)}
    error = _deliver_result(job, "Report: task two is open.", adapters={platform: adapter}, loop=MagicMock())
    assert error is None
    assert send.call_args.args[2]["cron_attach"] is True
    adapter.create_handoff_thread.assert_not_called()
    source = SessionSource(platform=platform, chat_id="target-channel", chat_type=chat_type,
                           thread_id="first-post", user_id="owner")
    entry = store.get_or_create_session(source)
    transcript = store.load_transcript(entry.session_id)
    assert any("Report: task two is open." in str(m.get("content")) for m in transcript)
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        db.append_message(entry.session_id, "user", "Task two is done.")
    finally:
        db.close()
    assert "Task two is done." in load_context("abcdef123456")


def test_telegram_private_target_keeps_existing_dm(delivery_env, monkeypatch):
    from cron.scheduler import _deliver_result
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.session import SessionSource
    home, _, store, adapter = delivery_env
    platform = Platform.TELEGRAM
    config = GatewayConfig(platforms={platform: PlatformConfig(enabled=True, token="test")})
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    send = AsyncMock(return_value=SendResult(success=True, message_id="accepted"))
    monkeypatch.setattr("gateway.delivery.DeliveryRouter._deliver_to_platform", send)
    source = SessionSource(platform=platform, chat_id="123456", chat_type="dm", user_id="123456")
    entry = store.get_or_create_session(source)
    topic = store.get_or_create_session(SessionSource(
        platform=platform, chat_id="123456", chat_type="dm",
        thread_id="newer-topic", user_id="123456"))
    from cron.context import save_context, load_context
    path = home / "cron" / "output" / "abcdef123456" / "2026-09-13_12-00-00.md"
    path.parent.mkdir(parents=True)
    path.write_text("log")
    save_context(path, "Your video is ready", True)
    job = {"id": "abcdef123456", "deliver": "telegram:123456", "attach_to_session": True,
           "_context_file": str(path)}
    assert _deliver_result(job, "Your video is ready", adapters={platform: adapter}, loop=MagicMock()) is None
    adapter.create_handoff_thread.assert_not_called()
    assert send.call_args.args[0].thread_id is None
    assert any("Your video is ready" in str(row.get("content")) for row in store.load_transcript(entry.session_id))
    assert not any("Your video is ready" in str(row.get("content")) for row in store.load_transcript(topic.session_id))
    from hermes_state import SessionDB
    db = SessionDB()
    db.append_message(topic.session_id, "user", "PRIVATE_TOPIC_REPLY")
    db.append_message(entry.session_id, "user", "Use a woodland scene tomorrow.")
    db.close()
    assert "Use a woodland scene tomorrow." in load_context("abcdef123456")
    assert "PRIVATE_TOPIC_REPLY" not in load_context("abcdef123456")


@pytest.mark.parametrize("root", [None, "existing-root"])
def test_native_scheduler_adapter_preserves_roots_and_media_when_reply_mode_off(delivery_env, monkeypatch, root):
    from cron.scheduler import _deliver_result
    from gateway.config import PlatformConfig
    from plugins.platforms.mattermost.adapter import MattermostAdapter
    home, platform, store, _ = delivery_env
    adapter = MattermostAdapter(PlatformConfig(enabled=True, token="fixture", extra={"url":"https://mm.invalid", "reply_mode":"off"}))
    adapter._session_store = store
    posts = []
    async def post(path, payload):
        posts.append(dict(payload))
        return {"id": f"post-{len(posts)}", "root_id": payload.get("root_id", "")}
    async def get(path):
        if path.startswith("channels/"):
            return {"id":"target-channel", "type":"P"}
        return {"id":path.split("/")[-1], "root_id":""}
    adapter._api_post = post
    adapter._api_get = get
    adapter._upload_file = AsyncMock(return_value="file-1")
    media = home / "report.txt"
    media.write_text("Fixture attachment")
    job = {"id":"abcdef123456", "deliver":"mattermost:target-channel", "attach_to_session":True}
    if root:
        job.update(deliver="origin", origin={"platform":"mattermost", "chat_id":"target-channel", "thread_id":root})
    error = _deliver_result(job, f"Briefing result.\nMEDIA:{media}", adapters={platform:adapter}, loop=MagicMock())
    assert error is None
    assert len(posts) == 2
    expected_root = root or "post-1"
    assert posts[0].get("root_id") == root
    assert posts[1]["root_id"] == expected_root
    assert posts[1]["file_ids"] == ["file-1"]


def test_attach_false_never_seeds_or_records(delivery_env, monkeypatch):
    from cron.scheduler import _deliver_result
    from gateway.platforms.base import SendResult
    home, platform, store, adapter = delivery_env
    send = AsyncMock(return_value=SendResult(success=True, message_id="new-post"))
    monkeypatch.setattr("gateway.delivery.DeliveryRouter._deliver_to_platform", send)
    seed = MagicMock()
    monkeypatch.setattr("cron.scheduler._seed_cron_thread_session", seed)
    job = {"id": "abcdef123456", "deliver": "mattermost:target-channel", "attach_to_session": False}
    assert _deliver_result(job, "Report", adapters={platform: adapter}, loop=MagicMock()) is None
    assert not send.call_args.args[2].get("cron_attach")
    seed.assert_not_called()


@pytest.mark.parametrize("chat_type", ["group", "channel", "dm"])
@pytest.mark.parametrize("per_user", [False, True])
@pytest.mark.parametrize("participant", ["owner", None])
def test_seed_matches_native_participant_reply(delivery_env, monkeypatch, chat_type, per_user, participant):
    from cron.scheduler import _deliver_result
    from gateway.platforms.base import SendResult
    home, platform, store, adapter = delivery_env
    store.config.thread_sessions_per_user = per_user
    send = AsyncMock(return_value=SendResult(success=True, message_id="report-root",
        raw_response={"cron_root_id": "report-root", "cron_chat_type": chat_type}))
    monkeypatch.setattr("gateway.delivery.DeliveryRouter._deliver_to_platform", send)
    job = {"id": "abcdef123456", "deliver": "origin", "attach_to_session": True,
           "origin": {"platform": "mattermost", "chat_id": "target-channel", "user_id": participant}}
    assert _deliver_result(job, "Seeded report", adapters={platform: adapter}, loop=MagicMock()) is None
    needs_user = chat_type != "dm" and per_user
    if needs_user and not participant:
        assert not store._entries
    else:
        reply = adapter.build_source(chat_id="target-channel", chat_type=chat_type,
                                     user_id=participant or "reader", thread_id="report-root")
        entry = store.get_or_create_session(reply)
        assert any("Seeded report" in row["content"] for row in store.load_transcript(entry.session_id))


@pytest.mark.parametrize("target", ["default", "other", "unserved"])
def test_seed_respects_receiving_native_profile_route(delivery_env, monkeypatch, tmp_path, target):
    from cron.scheduler import _deliver_result
    from gateway.platforms.base import SendResult
    from gateway.profile_routing import parse_profile_routes
    from gateway.run import GatewayRunner
    home, platform, store, adapter = delivery_env
    store.config.multiplex_profiles = True
    store.config.profile_routes = parse_profile_routes([
        {"name": "fixture", "platform": "mattermost", "chat_id": "target-channel", "profile": target}])
    monkeypatch.setattr("gateway.run._multiplex_profile_homes", lambda config: [("default", home), ("other", home / "other")])
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = store.config
    adapter.gateway_runner = runner
    send = AsyncMock(return_value=SendResult(success=True, message_id="report-root",
        raw_response={"cron_root_id": "report-root", "cron_chat_type": "dm"}))
    monkeypatch.setattr("gateway.delivery.DeliveryRouter._deliver_to_platform", send)
    job = {"id": "abcdef123456", "deliver": "mattermost:target-channel", "attach_to_session": True}
    assert _deliver_result(job, "Profile report", adapters={platform: adapter}, loop=MagicMock()) is None
    assert bool(store._entries) is (target == "default")


@pytest.mark.parametrize("damage", ["agent", "thread", "participant", "channel"])
def test_reused_entry_wrong_identity_is_not_seeded(delivery_env, monkeypatch, damage):
    from dataclasses import replace
    from cron.scheduler import _deliver_result
    from gateway.platforms.base import SendResult
    home, platform, store, adapter = delivery_env
    store.config.thread_sessions_per_user = True
    dest = adapter.build_source(chat_id="target-channel", chat_type="group", user_id="owner", thread_id="report-root")
    wrong = replace(dest, **{"agent": {"profile": "other"}, "thread": {"thread_id": "other"},
                            "participant": {"user_id": "other"}, "channel": {"chat_id": "other"}}[damage])
    entry = store.get_or_create_session(wrong)
    monkeypatch.setattr(store, "get_or_create_session", lambda source: entry)
    send = AsyncMock(return_value=SendResult(success=True, message_id="report-root",
        raw_response={"cron_root_id": "report-root", "cron_chat_type": "group"}))
    monkeypatch.setattr("gateway.delivery.DeliveryRouter._deliver_to_platform", send)
    job = {"id": "abcdef123456", "deliver": "origin", "attach_to_session": True,
           "origin": {"platform": "mattermost", "chat_id": "target-channel", "user_id": "owner"}}
    assert _deliver_result(job, "DO_NOT_SEED", adapters={platform: adapter}, loop=MagicMock()) is None
    assert not any("DO_NOT_SEED" in row["content"] for row in store.load_transcript(entry.session_id))


@pytest.mark.parametrize("legacy_store_key", [False, True])
@pytest.mark.parametrize("legacy_entry_key", [False, True])
def test_seed_canonicalizes_both_native_legacy_dm_keys(delivery_env, monkeypatch, legacy_store_key, legacy_entry_key):
    from cron.scheduler import _seed_cron_thread_session
    from gateway.session import SessionSource
    home, platform, store, adapter = delivery_env
    source = adapter.build_source(chat_id="target-channel", chat_type="dm", thread_id="report-root")
    entry = store.get_or_create_session(source)
    legacy = "agent:main:mattermost:dm:report-root"
    if legacy_entry_key:
        entry.session_key = legacy
        store._db.record_gateway_session_peer(entry.session_id, source="mattermost", session_key=legacy,
            chat_id=source.chat_id, chat_type=source.chat_type, thread_id=source.thread_id,
            origin_json=__import__("json").dumps(source.to_dict()))
    if legacy_store_key:
        monkeypatch.setattr(store, "_generate_session_key", lambda source: legacy)
    monkeypatch.setattr(store, "get_or_create_session", lambda source: entry)
    _seed_cron_thread_session({"id": "abcdef123456"}, adapter, "mattermost", "target-channel",
                             "report-root", "Legacy seed", chat_type="dm")
    assert any("Legacy seed" in row["content"] for row in store.load_transcript(entry.session_id))


def test_home_participant_seeds_isolated_thread(delivery_env, monkeypatch):
    from cron.scheduler import _deliver_result
    from gateway.config import HomeChannel
    from gateway.platforms.base import SendResult
    home, platform, store, adapter = delivery_env
    store.config.thread_sessions_per_user = True
    store.config.platforms[platform].home_channel = HomeChannel(
        platform=platform, chat_id="target-channel", name="Reports", user_id="home-owner", scope_id="home-team")
    send = AsyncMock(return_value=SendResult(success=True, message_id="report-root",
        raw_response={"cron_root_id": "report-root", "cron_chat_type": "group"}))
    monkeypatch.setattr("gateway.delivery.DeliveryRouter._deliver_to_platform", send)
    job = {"id": "abcdef123456", "deliver": "origin", "attach_to_session": True}
    assert _deliver_result(job, "Home report", adapters={platform: adapter}, loop=MagicMock()) is None
    assert next(iter(store._entries.values())).origin.scope_id == "home-team"
    reply = adapter.build_source(chat_id="target-channel", chat_type="group", user_id="home-owner", thread_id="report-root", scope_id="home-team")
    entry = store.get_or_create_session(reply)
    assert any("Home report" in row["content"] for row in store.load_transcript(entry.session_id))
    assert entry.origin.scope_id == "home-team"
