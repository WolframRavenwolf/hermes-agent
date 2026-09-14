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
def discussion_delivery(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from cron import jobs
    from gateway.config import GatewayConfig
    from gateway.session import SessionSource, SessionStore
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True)})
    store = SessionStore(tmp_path / "sessions", config)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=CHAT_ID, chat_type="dm", user_id="owner")
    flat = store.get_or_create_session(source)
    topic = store.get_or_create_session(SessionSource(
        platform=Platform.TELEGRAM, chat_id=CHAT_ID, chat_type="dm", user_id="owner", thread_id="99"))
    with jobs.use_cron_store(tmp_path):
        path = jobs.save_job_output("abcdef", "Native output document")
        job = {"id": "abcdef", "name": "Brief", "deliver": "origin", "attach_to_session": True,
               "origin": {"platform": "telegram", "chat_id": CHAT_ID, "chat_type": "dm", "user_id": "owner"},
               "_context_output_file": str(path)}
        try:
            yield job, path, store, SimpleNamespace(_session_store=store), flat, topic, config
        finally:
            store.close_all_db_handles()


@pytest.mark.parametrize("route", ["mirror", "thread", "channel"])
def test_native_mirror_and_seed_owners_bind_exact_persisted_user_turn(discussion_delivery, route):
    import json
    from cron.context import load_context
    job, path, store, adapter, flat, topic, _ = discussion_delivery
    if route == "mirror":
        sched_delivery._maybe_mirror_cron_delivery(job, "telegram", CHAT_ID, "Delivered report", user_id="owner", enabled=True)
    elif route == "thread":
        sched_delivery._seed_cron_thread_session(job, adapter, "telegram", CHAT_ID, "99", "Delivered report", is_dm=True)
    else:
        assert sched_delivery._seed_cron_channel_session(
            job, adapter, "telegram", CHAT_ID, "Delivered report", is_dm=True, user_id="owner")
    record = json.loads(path.with_suffix(".context.json").read_text())
    sid = topic.session_id if route == "thread" else flat.session_id
    assert len(record["conversations"]) == 1
    ref = record["conversations"][0]
    assert ref["session_id"] == sid
    rows = store._db.get_context_messages(sid, matching_content="[Cron delivery: Brief]\nDelivered report")
    assert ref == {"session_id": sid, "after_id": rows[0]["id"], "after_timestamp": rows[0]["timestamp"]}
    other = flat.session_id if route == "thread" else topic.session_id
    store._db.append_message(sid, "user", "Next briefing correction")
    store._db.append_message(other, "user", "OTHER CONVERSATION")
    result = load_context("abcdef")
    assert "Next briefing correction" in result and "OTHER CONVERSATION" not in result


@pytest.mark.parametrize("route", ["mirror", "seed"])
@pytest.mark.parametrize("failure", ["write", "missing_anchor"])
def test_failed_mirror_or_missing_anchor_never_records_reference(discussion_delivery, monkeypatch, route, failure):
    import gateway.mirror as mirror
    job, path, store, adapter, _, _, _ = discussion_delivery
    if failure == "write":
        def fail_write(*args, **kwargs):
            raise OSError("SQLite unavailable")
        monkeypatch.setattr(mirror, "_append_to_sqlite", fail_write)
    else:
        monkeypatch.setattr(mirror, "mirror_to_session", lambda *args, **kwargs: True)
    if route == "mirror":
        sched_delivery._maybe_mirror_cron_delivery(job, "telegram", CHAT_ID, "Undurable report", user_id="owner", enabled=True)
    else:
        sched_delivery._seed_cron_channel_session(job, adapter, "telegram", CHAT_ID, "Undurable report", is_dm=True, user_id="owner")
    assert not path.with_suffix(".context.json").exists()


def test_seed_without_exact_session_does_not_fall_back_to_existing_chat(discussion_delivery):
    from types import SimpleNamespace
    job, path, store, _, flat, _, _ = discussion_delivery
    assert not sched_delivery._seed_cron_session(
        job, SimpleNamespace(), "telegram", CHAT_ID, "Do not infer", thread_id=None,
        chat_type="dm", user_id="owner", chat_name=None, scope_id=None)
    assert store._db.get_context_messages(flat.session_id) == []
    assert not path.with_suffix(".context.json").exists()


def test_flat_mirror_does_not_select_only_available_topic(discussion_delivery):
    job, path, store, _, flat, topic, _ = discussion_delivery
    # Different chat has a topic but no flat conversation.
    from gateway.session import SessionSource
    entry = store.get_or_create_session(SessionSource(
        platform=Platform.TELEGRAM, chat_id="topic-only", chat_type="dm", user_id="owner", thread_id="99"))
    sched_delivery._maybe_mirror_cron_delivery(job, "telegram", "topic-only", "Do not infer", user_id="owner", enabled=True)
    assert store._db.get_context_messages(entry.session_id) == []
    assert not path.with_suffix(".context.json").exists()


@pytest.mark.parametrize("chat_type,thread_id,group_per_user,thread_per_user,participant,ended,allowed", [
    ("group", None, True, False, "B", False, False),
    ("group", None, True, False, "B", True, False),
    ("group", "99", True, True, "B", False, False),
    ("group", "99", True, True, "B", True, False),
    ("group", None, True, False, "A", False, True),
    ("group", None, False, False, "B", False, True),
    ("group", "99", True, False, "B", False, True),
    ("group", "99", False, True, "B", False, True),
    ("dm", None, True, False, "B", False, True),
    ("dm", "99", True, True, "B", False, True),
])
def test_standalone_discussion_uses_native_participant_route(
    discussion_delivery, monkeypatch, chat_type, thread_id, group_per_user,
    thread_per_user, participant, ended, allowed,
):
    import json
    from dataclasses import replace
    from cron.context import load_context
    from gateway.session import SessionSource
    job, path, store, _, _, _, config = discussion_delivery
    config.group_sessions_per_user = group_per_user
    config.thread_sessions_per_user = thread_per_user
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="participant-route",
                           chat_type=chat_type, thread_id=thread_id, user_id="A")
    if ended:
        previous = store.get_or_create_session(source)
        store._db.end_session(previous.session_id, "session_reset")
    target = store.get_or_create_session(replace(source, user_id=participant))
    assert (target.session_key == store._generate_session_key(source)) is allowed
    job["origin"] = source.to_dict()
    sent = []
    async def send(*args, **kwargs):
        sent.append((args, kwargs))
        return {"success": True, "message_id": "confirmed"}
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    monkeypatch.setattr("tools.send_message_tool._send_to_platform", send)
    monkeypatch.setattr(sched, "load_config", lambda: {"cron": {"wrap_response": False}})
    assert sched._deliver_result(job, "Delivered report") is None
    assert sent
    store._db.append_message(target.session_id, "user", "Participant route correction")
    result = load_context("abcdef")
    companion = path.with_suffix(".context.json")
    if allowed:
        record = json.loads(companion.read_text())
        assert record["conversations"][0]["session_id"] == target.session_id
        assert "Participant route correction" in result
    else:
        assert not companion.exists()
        assert result == ""


def test_discussion_rejects_same_user_in_another_slack_scope(discussion_delivery, monkeypatch):
    from gateway.session import SessionSource
    from cron.context import load_context
    job, path, store, _, _, _, config = discussion_delivery
    target = store.get_or_create_session(SessionSource(
        platform=Platform.SLACK, chat_id="scope-chat", chat_type="group", user_id="A", scope_id="B"))
    job["origin"] = {"platform": "slack", "chat_id": "scope-chat", "chat_type": "group",
                     "user_id": "A", "scope_id": "A"}
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    sched_delivery._maybe_mirror_cron_delivery(
        job, "slack", "scope-chat", "Delivered report", user_id="A", enabled=True)
    store._db.append_message(target.session_id, "user", "Other workspace correction")
    assert not path.with_suffix(".context.json").exists()
    assert load_context("abcdef") == ""


@pytest.mark.parametrize("delivered", [True, False])
def test_save_compose_deliver_reaches_real_sqlite_and_next_prompt(discussion_delivery, monkeypatch, delivered):
    from cron import jobs
    from cron.scheduler_prompt import _inject_context_from
    job, old_path, store, adapter, flat, _, config = discussion_delivery
    job.pop("_context_output_file")
    jobs.save_jobs([job])
    sent = []
    async def send(target, text, metadata):
        sent.append(text)
        return {"success": delivered, "delivered": delivered, "message_id": "123" if delivered else None}
    async def fail_standalone(*args, **kwargs):
        return {"error": "Transport unavailable"}
    router = MagicMock()
    router._deliver_to_platform = send
    loop = MagicMock()
    loop.is_running.return_value = True
    def run_coro(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:
            future.set_exception(exc)
        return future
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    monkeypatch.setattr(sched, "load_config", lambda: {"cron": {"wrap_response": False}})
    monkeypatch.setattr("gateway.delivery.DeliveryRouter", lambda *args, **kwargs: router)
    monkeypatch.setattr("tools.send_message_tool._send_to_platform", fail_standalone)
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", run_coro)
    outcome = sched._RunDelivery(job, success=True, error=None)
    sched._save_compose_deliver(outcome, sched._FireOwnership(job, None), "Delivered report", "Original native log",
                               adapters={Platform.TELEGRAM: adapter}, loop=loop, verbose=False, execution_token=None)
    assert sent and outcome.delivery_attempted
    assert "_context_output_file" not in job and "_context_output_file" not in jobs.get_job(job["id"])
    companions = list(old_path.parent.glob("*.context.json"))
    if delivered:
        assert outcome.delivery_error is None and len(companions) == 1
        store._db.append_message(flat.session_id, "user", "Correction through the native pipeline")
        prompt, injected = _inject_context_from({"id": "abcdef", "context_from": ["self"]}, "Next task")
        assert injected and "Original native log" in prompt and "Correction through the native pipeline" in prompt
    else:
        assert outcome.delivery_error and companions == []
