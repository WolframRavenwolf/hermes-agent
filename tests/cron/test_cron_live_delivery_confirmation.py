"""Live-adapter delivery confirmation for cron (#77763).

A ``no_agent`` job fired, the scheduler logged
``delivered to telegram:<chat> via live adapter``, and the user received
nothing — no message row, no delivery obligation. The log line was not
evidence of a send:

* the silence-narration filter returns ``{"success": True, "delivered": False}``
  (a successful *drop*), and the normalization block read only ``success``;
* an empty payload skipped the send entirely and still fell into the
  "delivered" branch;
* the log line named the chat but not the lane, so a wrong-thread delivery and
  a phantom one look identical after the fact.

These tests pin the confirmation contract: positive evidence, honest logging,
and fail-closed on nothing-to-send.
"""

import asyncio
import logging
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

from cron import scheduler as sched
from cron import scheduler_delivery as sched_delivery
from cron.scheduler import _deliver_result
from cron.scheduler_delivery import _confirm_adapter_delivery
from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# _confirm_adapter_delivery: the contract in isolation
# ---------------------------------------------------------------------------

class _SendResult:
    """Minimal stand-in for an adapter SendResult."""

    def __init__(self, success=True, message_id=None, raw_response=None, **extra):
        self.success = success
        self.message_id = message_id
        self.raw_response = raw_response
        for key, value in extra.items():
            setattr(self, key, value)


class TestConfirmAdapterDelivery:
    def test_none_is_not_delivered(self):
        assert _confirm_adapter_delivery(None, "j1") is False

    def test_missing_success_is_not_delivered(self):
        assert _confirm_adapter_delivery(object(), "j1") is False
        assert _confirm_adapter_delivery({"message_id": 7}, "j1") is False

    def test_explicit_failure_is_not_delivered(self):
        assert _confirm_adapter_delivery(_SendResult(success=False), "j1") is False
        assert _confirm_adapter_delivery({"success": False}, "j1") is False

    def test_filtered_dict_is_not_delivered(self):
        """The exact silence-filter shape: a successful DROP is not a delivery."""
        filtered = {"success": True, "filtered": "silence_narration", "delivered": False}
        assert _confirm_adapter_delivery(filtered, "j1") is False

    def test_delivered_false_on_an_object_is_not_delivered(self):
        result = _SendResult(success=True, message_id=42, delivered=False)
        assert _confirm_adapter_delivery(result, "j1") is False

    def test_positive_evidence_is_delivered_without_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            assert _confirm_adapter_delivery(_SendResult(message_id=1234), "j1") is True
        assert "UNVERIFIED" not in caplog.text

    def test_raw_response_alone_counts_as_evidence(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            result = _SendResult(raw_response={"ok": True})
            assert _confirm_adapter_delivery(result, "j1") is True
        assert "UNVERIFIED" not in caplog.text

    def test_evidence_free_success_is_accepted_but_warned(self, caplog):
        """Not proof of failure either — accept it, but say so in the log."""
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            assert _confirm_adapter_delivery(_SendResult(), "92e639af907f") is True
        assert "UNVERIFIED" in caplog.text
        assert "92e639af907f" in caplog.text

    def test_evidence_free_success_dict_is_accepted_but_warned(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            assert _confirm_adapter_delivery({"success": True}, "j1") is True
        assert "UNVERIFIED" in caplog.text


# ---------------------------------------------------------------------------
# _deliver_result: the live lane end to end
# ---------------------------------------------------------------------------

CHAT_ID = "-1001234567890"


def _job(thread_id=None):
    origin = {"platform": "telegram", "chat_id": CHAT_ID}
    if thread_id is not None:
        origin["thread_id"] = thread_id
    return {
        "id": "92e639af907f",
        "name": "Ghost Delivery",
        "deliver": "origin",
        "origin": origin,
    }


def _gateway_config(relay=False):
    config = MagicMock()
    platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True)}
    if relay:
        platforms[Platform.RELAY] = PlatformConfig(enabled=True)
    config.platforms = platforms
    config.get_home_channel = lambda p: None
    return config


def _adapters(relay=False):
    adapter = MagicMock()
    if relay:
        adapter.fronts_platform = lambda p: p == Platform.TELEGRAM
        return {Platform.RELAY: adapter}
    return {Platform.TELEGRAM: adapter}


RECORDED_VERIFICATION = []


def _record_verification(job, unverified_targets):
    RECORDED_VERIFICATION.append((job["id"], list(unverified_targets)))


def _run(job, content, send_result, relay=False, standalone_result=None, cron_cfg=None):
    """Drive ``_deliver_result`` over the live lane with a stubbed router.

    Returns ``(error, router_calls, standalone_calls)``. ``cron_cfg`` extends
    the ``cron:`` section handed to the scheduler (default: unwrapped output).
    """
    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as e:  # noqa: BLE001
            future.set_exception(e)
        return future

    router_calls = []
    standalone_calls = []
    RECORDED_VERIFICATION.clear()

    router = MagicMock()

    async def _deliver_to_platform(target, text, metadata):
        router_calls.append({"target": target, "text": text, "metadata": metadata})
        return send_result

    router._deliver_to_platform = _deliver_to_platform

    async def _fake_send_to_platform(platform, pconfig, chat_id, text, **kwargs):
        standalone_calls.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return standalone_result if standalone_result is not None else {}

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config(relay)), \
         patch("cron.scheduler.load_config",
               return_value={"cron": {"wrap_response": False, **(cron_cfg or {})}}), \
         patch("cron.scheduler_delivery._record_delivery_verification", side_effect=_record_verification), \
         patch("gateway.delivery.DeliveryRouter", return_value=router), \
         patch("tools.send_message_tool._send_to_platform", _fake_send_to_platform), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
        error = _deliver_result(job, content, adapters=_adapters(relay), loop=loop)
    return error, router_calls, standalone_calls


class TestFilteredResultIsNotDelivered:
    FILTERED = {"success": True, "filtered": "silence_narration", "delivered": False}

    def test_filtered_dict_does_not_log_a_live_delivery(self, caplog):
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            _, router_calls, standalone_calls = _run(_job(), "...", self.FILTERED)

        assert len(router_calls) == 1                     # the live send was attempted
        assert "via live adapter" not in caplog.text      # but never claimed as delivered
        assert len(standalone_calls) == 1                 # fell back instead of lying

    def test_filtered_dict_fails_closed_on_the_relay_lane(self):
        """Relay owns the destination, so there is no fallback — report it."""
        error, _, standalone_calls = _run(_job(), "...", self.FILTERED, relay=True)

        assert error is not None
        assert "unconfirmed result" in error
        assert "silence_narration" in error  # names the filter, not "unknown"
        assert standalone_calls == []

    def test_confirmed_send_result_still_delivers(self, caplog):
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            error, router_calls, standalone_calls = _run(
                _job(), "Nightly report.", _SendResult(message_id=1234),
            )

        assert error is None
        assert len(router_calls) == 1
        assert standalone_calls == []
        assert "via live adapter" in caplog.text


class TestEmptyPayloadFailsClosed:
    def test_empty_payload_never_reaches_the_adapter(self, caplog):
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            _, router_calls, _ = _run(_job(), "   ", _SendResult(message_id=1))

        assert router_calls == []                     # nothing was sent
        assert "via live adapter" not in caplog.text  # and nothing was claimed
        assert "empty text and no media" in caplog.text

    def test_empty_payload_never_reaches_the_standalone_sender(self, caplog):
        """The native fallback must not re-open the hole the live lane closed.

        Telegram's adapter returns ``SendResult(success=True)`` for empty
        content without an API call, so an unguarded fallback would log a
        standalone "delivered" for the same phantom payload (#77763).
        """
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            error, router_calls, standalone_calls = _run(
                _job(), "   ", _SendResult(message_id=1),
            )

        assert router_calls == []
        assert standalone_calls == []  # _send_to_platform never called
        assert error is not None
        assert "standalone send skipped (empty text and no media)" in error
        assert "delivered to" not in caplog.text

    def test_empty_payload_is_reported_on_the_relay_lane(self):
        error, router_calls, _ = _run(_job(), "", _SendResult(message_id=1), relay=True)

        assert router_calls == []
        assert error is not None
        assert "live adapter send skipped (empty text and no media)" in error


class TestDeliveredLogNamesTheLane:
    def test_log_includes_thread_and_message_id(self, caplog):
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            error, _, _ = _run(
                _job(thread_id="99"), "Nightly report.", _SendResult(message_id=1234),
            )

        assert error is None
        assert "via live adapter thread=99 message_id=1234" in caplog.text

    def test_log_uses_a_dash_when_the_lane_is_unknown(self, caplog):
        """No thread and an evidence-free result must still be attributable."""
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            error, _, _ = _run(_job(), "Nightly report.", _SendResult())

        assert error is None
        assert "via live adapter thread=- message_id=-" in caplog.text
        assert "UNVERIFIED" in caplog.text


class TestLiveDeliveryIsAFinalNotification:
    """Cron output is a final user-visible delivery, not a progress send.

    Telegram's adapter defaults to ``_notifications_mode = "important"`` and
    sends with ``disable_notification=True`` unless ``metadata["notify"]`` is
    set — so a cron brief without the marker lands silently, which users
    report as "never delivered" (#77763 thread, #58258 typing bubble). The
    marker must ride both the text route and the media route, in every
    Telegram routing mode.
    """

    def test_text_route_metadata_carries_notify(self):
        _, router_calls, _ = _run(_job(), "Nightly report.", _SendResult(message_id=1))
        assert len(router_calls) == 1
        metadata = router_calls[0]["metadata"]
        assert metadata["job_id"] == "92e639af907f"
        assert metadata["notify"] is True

    def test_forum_topic_route_keeps_thread_and_notify(self):
        _, router_calls, _ = _run(
            _job(thread_id="99"), "Nightly report.", _SendResult(message_id=1),
        )
        metadata = router_calls[0]["metadata"]
        assert metadata["thread_id"] == "99"
        assert metadata["notify"] is True

    def test_media_route_metadata_carries_notify(self, tmp_path):
        media = tmp_path / "report.png"
        media.write_bytes(b"\x89PNG\r\n\x1a\n")
        sent = []

        def fake_send_media(adapter, chat_id, media_files, metadata, loop, job, platform=None):
            sent.append({"media": list(media_files), "metadata": metadata})
            return []

        with patch("cron.scheduler_delivery._send_media_via_adapter", side_effect=fake_send_media), \
             patch("gateway.platforms.base.BasePlatformAdapter.filter_media_delivery_paths",
                   side_effect=lambda files: files):
            error, router_calls, _ = _run(
                _job(), f"Nightly report.\nMEDIA:{media}", _SendResult(message_id=1),
            )

        assert error is None
        assert len(router_calls) == 1
        assert len(sent) == 1
        assert sent[0]["metadata"]["notify"] is True


class TestNotifyIsConfigurable:
    """``cron.delivery.notify`` (config.yaml) gates the notify marker.

    The current behaviour (push notification) stays the default; only an
    explicit ``false`` restores silent deliveries. The knob rides both the
    text route and the media route so the two never disagree.
    """

    def test_default_is_notify(self):
        _, router_calls, _ = _run(_job(), "Nightly report.", _SendResult(message_id=1))
        assert router_calls[0]["metadata"]["notify"] is True

    def test_explicit_false_disables_notify_on_text_route(self):
        _, router_calls, _ = _run(
            _job(thread_id="99"), "Nightly report.", _SendResult(message_id=1),
            cron_cfg={"delivery": {"notify": False}},
        )
        metadata = router_calls[0]["metadata"]
        assert metadata["notify"] is False
        assert metadata["thread_id"] == "99"  # routing untouched

    def test_explicit_false_disables_notify_on_media_route(self, tmp_path):
        media = tmp_path / "report.png"
        media.write_bytes(b"\x89PNG\r\n\x1a\n")
        sent = []

        def fake_send_media(adapter, chat_id, media_files, metadata, loop, job, platform=None):
            sent.append(metadata)
            return []

        with patch("cron.scheduler_delivery._send_media_via_adapter", side_effect=fake_send_media), \
             patch("gateway.platforms.base.BasePlatformAdapter.filter_media_delivery_paths",
                   side_effect=lambda files: files):
            _run(
                _job(), f"Nightly report.\nMEDIA:{media}", _SendResult(message_id=1),
                cron_cfg={"delivery": {"notify": False}},
            )
        assert sent[0]["notify"] is False

    @pytest.mark.parametrize("cron_cfg", [
        {"delivery": None},            # `delivery:` with no body parses to null
        {"delivery": "yes"},           # malformed scalar
        {"delivery": {"notify": None}},  # `notify:` with no value
    ])
    def test_malformed_section_keeps_the_default(self, cron_cfg):
        _, router_calls, _ = _run(_job(), "Nightly report.", _SendResult(message_id=1), cron_cfg=cron_cfg)
        assert router_calls[0]["metadata"]["notify"] is True

    def test_default_config_ships_notify_true(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["cron"]["delivery"]["notify"] is True


class TestUnverifiedDeliveryIsRecordedOnTheJob:
    """An evidence-free ack is accepted, but the state must reach the job
    record (and from there ``hermes cron list`` / ``cron doctor``), not only a
    WARNING log line."""

    def test_evidence_free_ack_records_the_target(self):
        error, _, _ = _run(_job(), "Nightly report.", _SendResult())
        assert error is None
        assert RECORDED_VERIFICATION == [("92e639af907f", [f"telegram:{CHAT_ID}"])]

    def test_positive_evidence_clears_the_marker(self):
        error, _, _ = _run(_job(), "Nightly report.", _SendResult(message_id=1234))
        assert error is None
        assert RECORDED_VERIFICATION == [("92e639af907f", [])]

    def test_recorder_skips_the_write_when_nothing_changed(self):
        with patch("cron.jobs.update_job") as update_job:
            sched_delivery._record_delivery_verification({"id": "j1", "last_delivery_unverified": None}, [])
            update_job.assert_not_called()
            sched_delivery._record_delivery_verification({"id": "j1", "last_delivery_unverified": None}, ["slack:C1"])
            update_job.assert_called_once_with("j1", {"last_delivery_unverified": ["slack:C1"]})

    def test_recorder_clears_a_stale_marker(self):
        with patch("cron.jobs.update_job") as update_job:
            sched_delivery._record_delivery_verification({"id": "j1", "last_delivery_unverified": ["slack:C1"]}, [])
            update_job.assert_called_once_with("j1", {"last_delivery_unverified": None})

    def test_tool_listing_exposes_the_field(self):
        from tools.cronjob_tools import _format_job

        assert _format_job({"id": "j1", "name": "n", "prompt": "p",
                            "last_delivery_unverified": ["slack:C1"]})["last_delivery_unverified"] == ["slack:C1"]


def test_scheduler_module_exposes_the_confirmation_helper():
    """Guard the import surface the delivery block depends on."""
    assert callable(sched_delivery._confirm_adapter_delivery)


@pytest.fixture
def mattermost_cron(tmp_path, monkeypatch):
    """Native scheduler/router/adapter/store; fake the gateway loop and HTTP only."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from gateway.config import GatewayConfig, HomeChannel
    from gateway.session import SessionStore
    from plugins.platforms.mattermost.adapter import MattermostAdapter

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # The global test fixture pins one DB; these cases exercise native profile scopes.
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    pc = PlatformConfig(enabled=True, extra={"max_post_length": 500, "reply_mode": "off", "require_mention": False})
    pc.home_channel = HomeChannel(platform=Platform.MATTERMOST, chat_id="room", name="Reports")
    config = GatewayConfig(platforms={Platform.MATTERMOST: pc})
    store = SessionStore(home / "sessions", config)
    adapter = MattermostAdapter(pc)
    adapter._session_store = store
    adapter._bot_user_id = "bot"
    adapter._bot_username = "hermes"
    state = SimpleNamespace(adapter=adapter, store=store, config=config, posts=[], standalone=[],
                            channel_type="O", outcome="ok", fail_at=2, channel_id="room", metadata_calls=[])

    def response(body, status=201):
        resp = AsyncMock()
        resp.__aenter__.return_value = resp
        resp.status = status
        resp.json.return_value = body
        resp.text.return_value = "invalid root_id" if status == 404 else "rejected"
        return resp

    def get(url, **kwargs):
        if "/channels/" in url:
            if state.outcome == "metadata_exception":
                raise ValueError("metadata unavailable")
            return response({"id": state.channel_id, "type": state.channel_type})
        return response({"id": "explicit-root", "root_id": ""})

    def post(url, **kwargs):
        payload = kwargs["json"]
        state.posts.append(payload)
        n = len(state.posts)
        if state.outcome == "warning_only" and n in (1, 3):
            return response({}, 404 if n == 1 else 400)
        if state.outcome == "rejected" and n == state.fail_at:
            return response({}, 400)
        if state.outcome == "uncertain" and n == state.fail_at:
            resp = response({})
            resp.json.side_effect = TimeoutError()
            return resp
        if state.outcome == "exception" and n == state.fail_at:
            raise ValueError("transport failed")
        return response({"id": f"post-{n}", "channel_id": "other" if state.outcome == "wrong_post" else "room",
                         "root_id": payload.get("root_id", "")})

    adapter._session = MagicMock()
    adapter._session.get.side_effect = get
    adapter._session.post.side_effect = post
    adapter._upload_file = AsyncMock(return_value="file-id")
    native_send = adapter.send

    async def send(chat_id, content, reply_to=None, metadata=None):
        state.metadata_calls.append(dict(metadata or {}))
        if state.outcome == "filtered":
            return {"success": True, "filtered": "silence_narration", "delivered": False}
        return await native_send(chat_id, content, reply_to, metadata)

    adapter.send = send
    loop = MagicMock()
    loop.is_running.return_value = True

    def run_coro(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    async def standalone(*args, **kwargs):
        state.standalone.append((args, kwargs))
        return {"success": True, "message_id": "standalone"}

    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    monkeypatch.setattr(sched, "load_config", lambda: {"cron": {"wrap_response": False, "mirror_delivery": True}})
    monkeypatch.setattr(sched_delivery, "_record_delivery_verification", lambda *args: None)
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", run_coro)
    monkeypatch.setattr("tools.send_message_tool._send_to_platform", standalone)
    state.job = {"id": "abc123", "name": "Brief", "deliver": "origin", "attach_to_session": True,
                 "origin": {"platform": "mattermost", "chat_id": "room", "user_id": "member", "chat_type": "dm"}}
    state.adapters = {Platform.MATTERMOST: adapter}
    state.run = lambda text: _deliver_result(state.job, text, adapters=state.adapters, loop=loop)
    return state


@pytest.mark.parametrize("kind", ["D", "G", "P", "O"])
@pytest.mark.parametrize("per_user", [False, True])
def test_mattermost_first_report_resumes_exact_native_reply_session(mattermost_cron, tmp_path, kind, per_user):
    import json
    from unittest.mock import AsyncMock
    from gateway.session import build_session_key
    s = mattermost_cron
    s.channel_type = kind
    s.config.thread_sessions_per_user = per_user
    report = "a" * 500 + "b" * 500 + "tail"
    media = tmp_path / "report.txt"
    media.write_text("attachment")
    error = s.run(report + f"\nMEDIA:{media}")
    assert error is None
    assert s.metadata_calls[0]["cron_attach"] is True
    assert [p.get("root_id") for p in s.posts] == [None, "post-1", "post-1", "post-1"]
    assert s.posts[0]["message"] == report[:500]
    assert s.posts[-1]["file_ids"] == ["file-id"]
    assert len(s.store._entries) == 1
    seeded = next(iter(s.store._entries.values()))
    s.adapter.handle_message = AsyncMock()
    asyncio.run(s.adapter._handle_ws_event({"event": "posted", "data": {
        "channel_type": kind, "sender_name": "member", "post": json.dumps({
            "id": "reply", "user_id": "member", "channel_id": "room", "message": "Continue",
            "root_id": "post-1"})}}))
    source = s.adapter.handle_message.call_args.args[0].source
    assert seeded.session_key == build_session_key(source, thread_sessions_per_user=per_user)
    resumed = s.store.get_or_create_session(source)
    assert resumed.session_id == seeded.session_id
    assert [(m["role"], m["content"]) for m in s.store.load_transcript(resumed.session_id)] == [
        ("user", "[Cron delivery: Brief]\n" + report)]
    assert s.standalone == []


@pytest.mark.parametrize("outcome,fail_at,replay", [
    ("rejected", 1, True), ("rejected", 2, False), ("uncertain", 1, False),
    ("uncertain", 2, False), ("exception", 1, False), ("exception", 2, False),
    ("warning_only", 3, False), ("filtered", 1, False), ("wrong_post", 1, False),
])
def test_mattermost_unconfirmed_report_never_seeds_or_replays_accepted_content(
        mattermost_cron, tmp_path, outcome, fail_at, replay):
    s = mattermost_cron
    s.outcome, s.fail_at = outcome, fail_at
    if outcome == "warning_only":
        s.job["origin"]["thread_id"] = "explicit-root"
    media = tmp_path / "report.txt"
    media.write_text("attachment")
    error = s.run("x" * 500 + "tail" + f"\nMEDIA:{media}")
    assert bool(s.standalone) is replay
    assert not s.store._entries
    assert not s.adapter._upload_file.called
    if not replay:
        assert error
    if outcome in {"rejected", "uncertain", "exception"}:
        assert len(s.posts) == fail_at


@pytest.mark.parametrize("kind,channel_id,outcome", [
    ("X", "room", "ok"), ("D", "other", "ok"), ("O", "room", "metadata_exception")])
def test_mattermost_failed_metadata_never_seeds_or_replays(mattermost_cron, kind, channel_id, outcome):
    s = mattermost_cron
    s.channel_type, s.channel_id, s.outcome = kind, channel_id, outcome
    assert s.run("Report")
    assert len(s.posts) == 1
    assert not s.store._entries and not s.standalone


@pytest.mark.parametrize("boundary", ["opt_out", "broadcast", "explicit_without_opt_in"])
def test_mattermost_attach_boundaries_keep_flat_delivery(mattermost_cron, boundary):
    s = mattermost_cron
    if boundary == "opt_out":
        s.job["attach_to_session"] = False
    else:
        s.job.pop("origin")
        s.job["deliver"] = "all" if boundary == "broadcast" else "mattermost:room"
        if boundary == "explicit_without_opt_in":
            s.job.pop("attach_to_session")
    assert s.run("Report") is None
    assert len(s.posts) == 1 and "root_id" not in s.posts[0]
    assert not s.metadata_calls[0].get("cron_attach")
    assert not s.store._entries


@pytest.mark.parametrize("route", ["explicit", "home", "origin_fallback", "explicit_root"])
def test_mattermost_opted_in_targets_seed_verified_root(mattermost_cron, route):
    s = mattermost_cron
    s.channel_type = "D"
    if route == "explicit_root":
        s.job["origin"]["thread_id"] = "explicit-root"
    else:
        s.job.pop("origin")
        s.job["deliver"] = {"explicit": "mattermost:room", "home": "mattermost", "origin_fallback": "origin"}[route]
    assert s.run("Report") is None
    assert len(s.posts) == 1 and len(s.store._entries) == 1
    entry = next(iter(s.store._entries.values()))
    expected_root = "explicit-root" if route == "explicit_root" else "post-1"
    assert entry.session_key == f"agent:main:mattermost:dm:room:{expected_root}"
    assert s.posts[0].get("root_id") == (expected_root if route == "explicit_root" else None)


@pytest.mark.parametrize("kind,group_per_user,should_seed", [("O", True, False), ("D", True, True), ("G", False, True)])
def test_mattermost_missing_required_participant_has_no_orphan_seed(mattermost_cron, kind, group_per_user, should_seed):
    s = mattermost_cron
    s.channel_type = kind
    s.config.thread_sessions_per_user = True
    s.config.group_sessions_per_user = group_per_user
    s.job["origin"].pop("user_id")
    assert s.run("Report") is None
    assert bool(s.store._entries) is should_seed


def test_mattermost_empty_report_has_no_placeholder_or_seed(mattermost_cron):
    s = mattermost_cron
    assert s.run("   ")
    assert not s.posts and not s.store._entries and not s.standalone


@pytest.mark.parametrize("owner,target,route,shared,dedicated", [
    ("default", "reviews", "explicit", False, False),
    ("reviews", "reviews", "origin", True, False),
    ("reviews", "reviews", "home", True, False),
    ("reviews", "reviews", "explicit", False, True),
    ("default", "unserved", "origin", False, False),
])
def test_mattermost_cron_respects_native_profile_owner(
        mattermost_cron, monkeypatch, tmp_path, caplog, owner, target, route, shared, dedicated):
    import json
    from pathlib import Path
    from unittest.mock import AsyncMock
    from cron.scheduler_preflight import SharedRouteAdapters
    from gateway.profile_routing import parse_profile_routes
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from hermes_cli.profiles import get_active_profile_name

    s = mattermost_cron
    home = s.store.sessions_dir.parent
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    reviews = home / "profiles" / "reviews"
    reviews.mkdir(parents=True)
    (reviews / "config.yaml").write_text("{}\n")
    s.config.multiplex_profiles = True
    s.config.profile_routes = parse_profile_routes([
        {"name": "reports", "platform": "mattermost", "chat_id": "room", "profile": target}])
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = s.config
    runner.adapters = {} if dedicated else {Platform.MATTERMOST: s.adapter}
    runner._profile_adapters = {"reviews": {Platform.MATTERMOST: s.adapter}} if dedicated else {}
    s.adapter.gateway_runner = runner
    if dedicated:
        s.adapter.set_owner_profile("reviews")
    if shared:
        s.adapters = SharedRouteAdapters(s.adapters, s.config.profile_routes)
    s.channel_type = "D"
    if route != "origin":
        s.job.pop("origin")
        s.job["deliver"] = "origin" if route == "home" else "mattermost:room"
    owner_home = home if owner == "default" else reviews
    with _profile_runtime_scope(owner_home, prepared_secret_scope={}):
        assert get_active_profile_name() == owner
        assert s.run("Profile report") is None
        assert len(s.posts) == 1 and not s.standalone
        s.adapter.handle_message = AsyncMock()
        asyncio.run(s.adapter._handle_ws_event({"event": "posted", "data": {
            "channel_type": "D", "post": json.dumps({"id": "reply", "user_id": "member",
                "channel_id": "room", "message": "Continue", "root_id": "post-1"})}}))
        source = s.adapter.handle_message.call_args.args[0].source
        assert runner._transport_owner(source) == (s.adapter, "reviews" if dedicated else None)
        if owner != target:
            assert not s.store._entries
            assert "thread seed did NOT land" in caplog.text
            for profile_home in (home, reviews):
                with _profile_runtime_scope(profile_home, prepared_secret_scope={}):
                    assert s.store._db.list_sessions_rich() == []
        else:
            assert source.profile == owner
            seeded = next(iter(s.store._entries.values()))
            assert seeded.session_key == s.store._generate_session_key(source)
            resumed = s.store.get_or_create_session(source)
            assert resumed.session_id == seeded.session_id
            assert [(m["role"], m["content"]) for m in s.store.load_transcript(resumed.session_id)] == [
                ("user", "[Cron delivery: Brief]\nProfile report")]
