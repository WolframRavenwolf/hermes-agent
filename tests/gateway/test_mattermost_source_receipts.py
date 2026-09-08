"""Source receipts through real Mattermost HTTP, stream and final-delivery owners."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from gateway.config import Platform, PlatformConfig
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from plugins.platforms.mattermost.adapter import MattermostAdapter


def _http_response(status=201, data=None, error=None):
    response = AsyncMock()
    response.__aenter__.return_value = response
    response.status = status
    response.json.return_value = data or {}
    response.json.side_effect = error
    response.text.return_value = "invalid message"
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["send", "edit"])
@pytest.mark.parametrize("outcome", ["lost", "rejected"])
async def test_source_receipt_separates_confirmed_attempted_and_unattempted(operation, outcome):
    adapter = MattermostAdapter(PlatformConfig(extra={"max_post_length": 500, "reply_mode": "off"}))
    requests = []
    def post(url, **kwargs):
        requests.append(kwargs["json"])
        n = len(requests) + (operation == "edit")
        if n == 2:
            return _http_response(201 if outcome == "lost" else 400,
                                  error=asyncio.TimeoutError() if outcome == "lost" else None)
        return _http_response(data={"id": f"post-{n}"})
    adapter._session = MagicMock()
    adapter._session.post.side_effect = post
    adapter._session.put.return_value = _http_response(200, {"id": "post-1"})
    content = "a" * 500 + "b" * 500 + "c" * 200
    result = (await adapter.send("channel", content) if operation == "send" else
              await adapter.edit_message("channel", "post-1", content))
    assert result.success is False
    assert result.message_id == "post-1"
    raw = result.raw_response or {}
    assert raw.get("source_confirmed_prefix") == content[:500]
    assert raw.get("source_attempted_prefix") == content[:1000 if outcome == "lost" else 500]
    assert bool(raw.get("_delivery_uncertain")) == (outcome == "lost")
    assert not any("c" in p["message"] for p in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("other_rejected", [False, True])
async def test_other_request_cannot_change_lost_ack_classification(other_rejected):
    adapter = MattermostAdapter(PlatformConfig(extra={"reply_mode": "thread"}))
    posts = []
    async def lost_ack():
        await adapter.send("other", "unrelated")
        raise aiohttp.ServerDisconnectedError()
    def post(url, **kwargs):
        posts.append(kwargs["json"])
        if posts[-1]["channel_id"] == "other":
            return _http_response(400 if other_rejected else 201, {"id": "other"})
        return _http_response(error=lost_ack)
    adapter._session = MagicMock()
    adapter._session.post.side_effect = post
    adapter._session.get.return_value = _http_response(200, {"root_id": "root"})
    result = await adapter.send("channel", "uncertain", metadata={"thread_id": "root", "notify": True})
    assert not result.success
    assert result.raw_response.get("_delivery_uncertain") is True
    assert result.raw_response["source_attempted_prefix"] == "uncertain"
    assert len(posts) == 2


@pytest.mark.parametrize("scenario", [
    "later", "during", "overflow", "flush", "segment", "decorated",
    "changed", "changed_lost", "changed_rejected", "changed_long_lost",
    "flush_final", "long_tail", "tail_lost", "media", "overflow_media", "overflow_changed",
    "overflow_media_whitespace", "overflow_media_newlines", "overflow_media_mixed",
])
@pytest.mark.asyncio
async def test_interim_receipt_preserves_later_final_without_replay(scenario):
    import threading
    from types import SimpleNamespace
    from gateway.session import SessionSource
    from gateway.stream_consumer import _FLUSH
    from tests.gateway.test_stale_finalize_suppression import _make_runner

    adapter = MattermostAdapter(PlatformConfig(extra={"max_post_length": 500, "reply_mode": "thread"}))
    posts, edits = [], []
    pending, release, overflow_acked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    correction_pending, correction_release = asyncio.Event(), asyncio.Event()
    changed = "changed" in scenario
    media_whitespace = scenario in {"overflow_media_whitespace", "overflow_media_newlines", "overflow_media_mixed"}
    whitespace = {
        "overflow_media_whitespace": " " * 10,
        "overflow_media_newlines": "\n" * 3,
        "overflow_media_mixed": "\n" * 6 + " \t",
    }.get(scenario, "")
    loss_at = 2 if media_whitespace else (3 if scenario.startswith("overflow") else 1)
    async def lost_ack():
        pending.set()
        await release.wait()
        raise asyncio.TimeoutError()
    def post(url, **kwargs):
        assert url.endswith("/api/v4/posts")
        posts.append(kwargs["json"])
        response = AsyncMock()
        response.__aenter__.return_value = response
        response.status = 201
        response.json.return_value = {"id": f"post-{len(posts)}"}
        if len(posts) == loss_at:
            response.json.side_effect = lost_ack
        elif scenario.startswith("overflow") and len(posts) == 2:
            async def overflow_ack():
                overflow_acked.set()
                return {"id": "post-2"}
            response.json.side_effect = overflow_ack
        elif scenario == "tail_lost" and len(posts) == 2:
            response.json.side_effect = asyncio.TimeoutError()
        elif posts[-1]["message"].startswith("Correction:"):
            if scenario == "changed_lost":
                async def correction_ack():
                    correction_pending.set()
                    await correction_release.wait()
                    raise asyncio.TimeoutError()
                response.json.side_effect = correction_ack
            elif scenario == "changed_long_lost":
                response.json.side_effect = asyncio.TimeoutError()
            elif scenario == "changed_rejected":
                response.status = 400
                response.text.return_value = "invalid message"
        return response
    def put(url, **kwargs):
        edits.append((url, kwargs["json"]))
        response = AsyncMock()
        response.__aenter__.return_value = response
        response.status = 200
        response.json.return_value = {"id": url.split("/")[-2]}
        return response
    adapter._session = MagicMock()
    adapter._session.post.side_effect = post
    adapter._session.put.side_effect = put
    adapter._api_get = AsyncMock(return_value={"root_id": "root"})
    adapter.delete_message = AsyncMock()
    cursor = "▉" if scenario == "decorated" else ""
    consumer = GatewayStreamConsumer(adapter, "channel", metadata={"thread_id": "root"},
        config=StreamConsumerConfig(cursor=cursor, edit_interval=0.01, buffer_threshold=1))
    prefix = "a" * 200
    if scenario == "decorated":
        prefix = "  ```text\n![image](https://example.com/image) (1/2) \t\n" + "a" * 100
    if scenario in {"media", "overflow_media"}:
        prefix = "MEDIA:/tmp/receipt-image.png\n\n" + prefix
    if media_whitespace:
        prefix = "[[audio_as_voice]]" + prefix
    consumer.on_delta(prefix)
    task = asyncio.create_task(consumer.run())
    if scenario.startswith("overflow"):
        # A confirmed preview AND a successful overflow precede the lost ack.
        while not posts:
            await asyncio.sleep(0)
        if media_whitespace:
            addition = "c" * 900 + whitespace
            consumer.on_delta(addition)
        else:
            addition = "c" * 150 + "\n\n" + "d" * 200
            consumer.on_delta(addition)
            await asyncio.wait_for(overflow_acked.wait(), 2)
            addition += "e" * 300
            consumer.on_delta("e" * 300)
        prefix += addition
    await asyncio.wait_for(pending.wait(), 2)
    tail = "b" * (1200 if scenario in {"long_tail", "tail_lost", "changed_long_lost"} else 100)
    full = ("replacement " + "z" * 180 + tail) if changed else prefix + tail
    if scenario == "during":
        consumer.on_delta(tail)
        consumer.finish(full)
    release.set()
    async def classified():
        while not getattr(consumer, "_delivery_ambiguous", False) and not task.done():
            await asyncio.sleep(0)
    await asyncio.wait_for(classified(), 2)
    interim_confirmed = consumer.final_content_delivered
    receipt = consumer._source_receipt
    assert receipt["source"] == consumer._clean_for_display(prefix)
    if media_whitespace:
        sealed = edits[0][1]["message"]
        uncertain = posts[loss_at - 1]["message"]
        assert sealed.startswith("a" * 200)
        assert uncertain == "c" * 500
        assert receipt["confirmed_end"] == len(sealed)
        assert receipt["attempted_end"] == len(sealed) + len(uncertain)
    else:
        assert receipt["attempted_end"] == len(consumer._clean_for_display(prefix))
        assert receipt["confirmed_end"] == (receipt["attempted_end"] - len(posts[loss_at - 1]["message"])
                                             if scenario.startswith("overflow") else 0)
    if scenario != "during":
        consumer.on_delta(tail)
        if scenario in {"flush", "segment", "flush_final"}:
            flush_event = threading.Event()
            consumer.on_delta(None) if scenario == "segment" else consumer._queue.put((_FLUSH, flush_event))
            # Flush both forms before introducing the next independent segment.
            if scenario == "segment":
                consumer._queue.put((_FLUSH, flush_event))
            async def flushed():
                while not flush_event.is_set() and not task.done():
                    await asyncio.sleep(0)
            await asyncio.wait_for(flushed(), 2)
            assert flush_event.is_set()
            assert posts[-1]["message"] == tail
            if scenario != "flush_final":
                full = "new segment answer"
                consumer.on_delta(full)
        consumer.finish(full)
    await asyncio.wait_for(task, 2)
    edit_count = len(edits)
    runner = _make_runner(adapter)
    source = SessionSource(platform=adapter.platform, chat_id="channel", chat_type="group", thread_id="root")
    turn = SimpleNamespace(stream_consumer_holder=[consumer], source=source, session_key="receipt-turn")
    response = {"final_response": full, "messages": [{"role": "assistant", "content": full}],
                "response_transformed": changed}
    if scenario == "changed_lost":
        reconcile = asyncio.create_task(runner._run_agent_mark_streamed_delivery(response, turn))
        await asyncio.wait_for(correction_pending.wait(), 2)
        concurrent_response = {"final_response": full}
        await runner._run_agent_mark_streamed_delivery(concurrent_response, turn)
        assert concurrent_response["already_sent"] and len(posts) == loss_at + 1
        correction_release.set()
        await asyncio.wait_for(reconcile, 2)
    else:
        await runner._run_agent_mark_streamed_delivery(response, turn)
    count = len(posts)
    await runner._run_agent_mark_streamed_delivery(response, turn)
    assert len(posts) == count and len(edits) == edit_count
    assert response["already_sent"]
    if changed:
        assert "".join(p["message"] for p in posts[loss_at:]) == (
            "Correction: Earlier text may appear again because delivery was not confirmed.\n\n" + full)
    elif media_whitespace:
        # Observe the complete run + HTTP-201 lost-ack + gateway reconciliation,
        # not just a replica of the receipt-offset calculation.
        remaining = "".join(p["message"] for p in posts[loss_at:])
        expected_whitespace = {
            "overflow_media_whitespace": " " * 10,
            "overflow_media_newlines": "\n\n",
            "overflow_media_mixed": "\n\n \t",
        }[scenario]
        expected = "c" * (900 - sealed.count("c") - uncertain.count("c")) + expected_whitespace + tail
        assert remaining == expected, (
            f"R1: remaining={len(remaining)}, expected={len(expected)}, "
            f"confirmed_end={receipt['confirmed_end']}, attempted_end={receipt['attempted_end']}"
        )
        assert edits[0][1]["message"] + posts[1]["message"] + remaining == consumer._clean_for_display(full)
    elif scenario not in {"flush", "segment"}:
        assert "".join(p["message"] for p in posts[loss_at:]) == tail
    assert not interim_confirmed, "an interim lost ack must not confirm final content"
    assert consumer._queue.empty()
    if scenario.startswith("overflow"):
        assert len(edits) == (1 if media_whitespace else 2)
        assert consumer._preview_message_ids == ({"post-1", "post-3"} if media_whitespace else {"post-1", "post-2", "post-4"})
        assert consumer.message_id == ("post-3" if media_whitespace else "post-4")
    adapter.delete_message.assert_not_called()
    assert all(len(p["message"]) <= 500 and p["root_id"] == "root" and
               p["props"]["disable_mentions"] for p in posts)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["stale", "stopped", "cancelled_suffix"])
async def test_source_reconciliation_obeys_turn_ownership(state):
    from tests.gateway.test_stale_finalize_suppression import _make_runner
    adapter = MattermostAdapter(PlatformConfig(extra={"max_post_length": 500, "reply_mode": "off"}))
    posts = []
    pending = asyncio.Event()
    async def pending_ack():
        pending.set()
        await asyncio.Event().wait()
    def post(url, **kwargs):
        posts.append(kwargs["json"])
        if len(posts) == 1:
            return _http_response(error=asyncio.TimeoutError())
        return _http_response(error=pending_ack)
    adapter._session = MagicMock()
    adapter._session.post.side_effect = post
    runner = _make_runner(adapter)
    generation = runner._begin_session_run_generation("owned")
    stopped = False
    consumer = GatewayStreamConsumer(adapter, "channel", config=StreamConsumerConfig(cursor=""),
        run_still_current=lambda: not stopped and runner._is_session_run_current("owned", generation))
    assert not await consumer._send_or_edit("prefix")
    assert consumer.source_delivery_pending
    if state == "stale":
        runner._begin_session_run_generation("owned")
    elif state == "stopped":
        stopped = True
    if state != "cancelled_suffix":
        assert not await consumer.reconcile_source_final("prefix tail")
        assert len(posts) == 1
        assert consumer._handled_final_source is None
        return
    task = asyncio.create_task(consumer.reconcile_source_final("prefix tail"))
    await asyncio.wait_for(pending.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await consumer.reconcile_source_final("prefix tail")
    assert [p["message"] for p in posts] == ["prefix", " tail"]
    assert consumer._source_receipt["attempted_end"] == len("prefix tail")


@pytest.mark.asyncio
async def test_source_receipts_leave_non_mattermost_final_delivery_unchanged():
    from gateway.session import SessionSource
    from tests.gateway.test_stale_finalize_suppression import FinalizeCaptureAdapter, _make_runner
    adapter = FinalizeCaptureAdapter()
    consumer = GatewayStreamConsumer(adapter, "channel", config=StreamConsumerConfig(cursor=""))
    consumer.on_delta("complete answer")
    consumer.finish("complete answer")
    await consumer.run()
    runner = _make_runner(adapter)
    response = {"final_response": "complete answer"}
    turn = SimpleNamespace(stream_consumer_holder=[consumer],
                           source=SessionSource(platform=Platform.TELEGRAM, chat_id="channel"), session_key="control")
    await runner._run_agent_mark_streamed_delivery(response, turn)
    assert response["already_sent"]
    assert not consumer.source_delivery_pending
    assert consumer._source_receipt is None
    assert [p["content"] for p in adapter.sent] == ["complete answer"]


@pytest.mark.asyncio
async def test_reconciled_reply_uses_real_native_media_delivery_without_body_replay(tmp_path):
    from tests.gateway.test_compression_exhaustion_reset_policy import _make_runner, _event_and_source
    runner, entry = _make_runner("none")
    event, source = _event_and_source()
    adapter = MattermostAdapter(PlatformConfig(extra={"reply_mode": "thread"}))
    runner.adapters = {Platform.MATTERMOST: adapter}
    posts, uploads = [], []
    def post(url, **kwargs):
        if url.endswith("/files"):
            uploads.append(kwargs["data"])
            return _http_response(data={"file_infos": [{"id": "file-1"}]})
        if not url.endswith("/posts"):
            return _http_response(data={"id": "typing"})
        posts.append(kwargs["json"])
        return _http_response(data={"id": f"post-{len(posts)}"},
                              error=asyncio.TimeoutError() if len(posts) == 1 else None)
    adapter._session = MagicMock()
    adapter._session.post.side_effect = post
    adapter._session.get.return_value = _http_response(200, {"root_id": source.thread_id})
    media = tmp_path / "receipt.pdf"
    media.write_bytes(b"%PDF-1.4 test fixture")
    final = "prefix tail\n\nMEDIA:" + str(media)
    consumer = GatewayStreamConsumer(adapter, source.chat_id, metadata={"thread_id": source.thread_id},
                                     config=StreamConsumerConfig(cursor=""))
    assert not await consumer._send_or_edit("prefix")
    response = {"final_response": final, "messages": [{"role": "assistant", "content": final}],
                "history_offset": 0, "last_prompt_tokens": 0}
    turn = SimpleNamespace(stream_consumer_holder=[consumer], source=source, session_key=entry.session_key)
    await runner._run_agent_mark_streamed_delivery(response, turn)
    assert response["already_sent"]
    # Replace model execution only; use the real admission/final/media chain.
    runner._run_agent = AsyncMock(return_value=response)
    returned = await runner._handle_message_with_agent(event, source, entry.session_key, 1)
    assert returned is None
    assert event._streamed_final_response == final
    assert response["messages"][0]["content"] == final
    assert [p["message"] for p in posts[:2]] == ["prefix", " tail"]
    assert len(uploads) == 1
    file_posts = [p for p in posts if p.get("file_ids")]
    assert [p["file_ids"] for p in file_posts] == [["file-1"]]
    assert not any(final in p["message"] for p in posts)
    assert all(p.get("root_id") == source.thread_id for p in posts)


@pytest.mark.asyncio
async def test_non_mattermost_failed_edit_remains_disabled_across_segment_reset():
    from gateway.platforms.base import SendResult
    from tests.gateway.test_stale_finalize_suppression import FinalizeCaptureAdapter
    class FailingEditAdapter(FinalizeCaptureAdapter):
        async def edit_message(self, *args, **kwargs):
            self.edits.append(kwargs)
            return SendResult(success=False, error="editing unavailable")
    adapter = FailingEditAdapter()
    consumer = GatewayStreamConsumer(adapter, "channel", config=StreamConsumerConfig(cursor=""))
    assert await consumer._send_or_edit("first preview")
    assert not await consumer._send_or_edit("first update")
    before_reset = len(adapter.edits)
    consumer._reset_segment_state()
    assert await consumer._send_or_edit("second preview")
    assert not await consumer._send_or_edit("second update")
    assert len(adapter.edits) == before_reset


@pytest.mark.asyncio
async def test_stale_source_receipt_cannot_fall_through_to_transformed_replay():
    from gateway.session import SessionSource
    from tests.gateway.test_stale_finalize_suppression import _make_runner
    adapter = MattermostAdapter(PlatformConfig(extra={"max_post_length": 500, "reply_mode": "off"}))
    adapter._session = MagicMock()
    adapter._session.post.side_effect = [_http_response(data={"id": "preview"}),
                                         _http_response(error=asyncio.TimeoutError())]
    adapter._session.put.return_value = _http_response(200, {"id": "preview"})
    runner = _make_runner(adapter)
    generation = runner._begin_session_run_generation("owned")
    consumer = GatewayStreamConsumer(adapter, "channel", config=StreamConsumerConfig(cursor=""),
        run_still_current=lambda: runner._is_session_run_current("owned", generation))
    assert await consumer._send_or_edit("preview")
    assert not await consumer._send_or_edit("a" * 500 + "b" * 300)
    assert consumer.source_delivery_pending
    runner._begin_session_run_generation("owned")
    before = adapter._session.put.call_count
    response = {"final_response": "changed final", "response_transformed": True}
    turn = SimpleNamespace(stream_consumer_holder=[consumer],
        source=SessionSource(platform=Platform.MATTERMOST, chat_id="channel"), session_key="owned")
    await runner._run_agent_mark_streamed_delivery(response, turn)
    assert adapter._session.put.call_count == before
    assert adapter._session.post.call_count == 2
    assert not response.get("already_sent")
