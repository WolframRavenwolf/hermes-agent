"""Cron conversation receipts through the real Mattermost send path."""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.mattermost.adapter import MattermostAdapter


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.delenv("MATTERMOST_MAX_POST_LENGTH", raising=False)
    adapter = MattermostAdapter(PlatformConfig(
        enabled=True,
        token="test-token",
        extra={
            "url": "https://mattermost.example.com",
            "max_post_length": 500,
            "reply_mode": "off",
        },
    ))
    state = SimpleNamespace(adapter=adapter, posts=[], channel_type="D")

    async def api_get(path):
        if path == "channels/channel-1":
            return {"id": "channel-1", "type": state.channel_type}
        if path == "posts/existing-reply":
            return {"id": "existing-reply", "root_id": "existing-root"}
        if path == "posts/existing-root":
            return {"id": "existing-root", "root_id": ""}
        raise AssertionError(f"Unexpected GET: {path}")

    async def api_post(path, payload):
        assert path == "posts"
        state.posts.append(dict(payload))
        return {
            "id": f"post-{len(state.posts)}",
            "root_id": payload.get("root_id", ""),
        }

    adapter._api_get = AsyncMock(side_effect=api_get)
    adapter._api_post = AsyncMock(side_effect=api_post)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_mode", ["off", "thread"])
@pytest.mark.parametrize("channel_type,chat_type", [("D", "dm"), ("O", "channel"), ("P", "group")])
@pytest.mark.parametrize("body", ["Scheduled result", "abcdef" * 200], ids=["single", "chunked"])
async def test_cron_uses_first_real_post_as_root(wire, reply_mode, channel_type, chat_type, body):
    wire.adapter._reply_mode = reply_mode
    wire.channel_type = channel_type
    metadata = {"cron_attach": True}

    result = await wire.adapter.send("channel-1", body, metadata=metadata)

    assert result.success is True
    assert "root_id" not in wire.posts[0]
    assert all(post["root_id"] == "post-1" for post in wire.posts[1:])
    assert len(wire.posts) == (1 if len(body) <= 500 else 3)
    assert all(post["channel_id"] == "channel-1" for post in wire.posts)
    assert all(post["props"]["disable_mentions"] is True for post in wire.posts)
    assert all(len(post["message"]) <= 500 for post in wire.posts)
    assert "".join(re.sub(r" \(\d+/\d+\)$", "", post["message"]) for post in wire.posts) == body
    ids = tuple(f"post-{index + 1}" for index in range(len(wire.posts)))
    assert result.message_id == ids[-1]
    assert result.continuation_message_ids == ids[:-1]
    assert result.raw_response == {
        "message_ids": ids,
        "source_confirmed_prefix": body,
        "source_attempted_prefix": body,
        "cron_root_id": ids[0],
        "cron_chat_type": chat_type,
    }
    wire.adapter._api_get.assert_awaited_once_with("channels/channel-1")
    assert metadata == {"cron_attach": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("root_source", ["reply_to", "thread_id", "root_id"])
@pytest.mark.parametrize("body", ["Scheduled result", "abcdef" * 200], ids=["single", "chunked"])
async def test_cron_receipt_uses_resolved_existing_root(wire, root_source, body):
    metadata: dict = {"cron_attach": True, "mattermost_explicit_thread": True}
    kwargs = {}
    if root_source == "reply_to":
        kwargs["reply_to"] = "existing-reply"
    else:
        metadata[root_source] = "existing-reply"

    result = await wire.adapter.send("channel-1", body, metadata=metadata, **kwargs)

    assert result.success is True
    assert all(post["root_id"] == "existing-root" for post in wire.posts)
    ids = tuple(f"post-{index + 1}" for index in range(len(wire.posts)))
    assert result.message_id == ids[-1]
    assert result.continuation_message_ids == ids[:-1]
    assert result.raw_response == {
        "message_ids": ids,
        "source_confirmed_prefix": body,
        "source_attempted_prefix": body,
        "cron_root_id": "existing-root",
        "cron_chat_type": "dm",
    }
    assert wire.adapter._api_get.await_args_list == [
        call("posts/existing-reply"), call("channels/channel-1"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, {}, {"cron_attach": False}])
@pytest.mark.parametrize("reply_mode", ["off", "thread"])
async def test_attach_disabled_keeps_flat_chunks(wire, metadata, reply_mode):
    wire.adapter._reply_mode = reply_mode

    result = await wire.adapter.send("channel-1", "abcdef" * 200, metadata=metadata)

    assert result.success is True
    assert len(wire.posts) == 3
    assert all("root_id" not in post for post in wire.posts)
    assert result.message_id == "post-3"
    assert result.continuation_message_ids == ("post-1", "post-2")
    assert result.raw_response == {
        "message_ids": ("post-1", "post-2", "post-3"),
        "source_confirmed_prefix": "abcdef" * 200,
        "source_attempted_prefix": "abcdef" * 200,
    }
    wire.adapter._api_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_interactive_explicit_thread_keeps_original_receipt(wire):
    wire.adapter._reply_mode = "thread"

    result = await wire.adapter.send("channel-1", "abcdef" * 200, reply_to="existing-reply")

    assert result.success is True
    assert len(wire.posts) == 3
    assert all(post["root_id"] == "existing-root" for post in wire.posts)
    assert result.raw_response == {
        "message_ids": ("post-1", "post-2", "post-3"),
        "source_confirmed_prefix": "abcdef" * 200,
        "source_attempted_prefix": "abcdef" * 200,
    }
    wire.adapter._api_get.assert_awaited_once_with("posts/existing-reply")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [{}, TimeoutError("post outcome unknown")], ids=["rejected", "timeout"])
async def test_failed_cron_chunk_keeps_partial_receipt_without_replay(wire, failure):
    wire.adapter._api_post.side_effect = [{"id": "post-1", "root_id": ""}, failure]

    result = await wire.adapter.send("channel-1", "abcdef" * 200, metadata={"cron_attach": True})

    assert result.success is False
    assert result.message_id == "post-1"
    assert result.continuation_message_ids == ("post-1",)
    assert result.raw_response == {
        "message_ids": ("post-1",),
        "source_confirmed_prefix": ("abcdef" * 200)[:500],
        "source_attempted_prefix": ("abcdef" * 200)[:1000 if isinstance(failure, TimeoutError) else 500],
        **({"_delivery_uncertain": True, "content_uncertain": True} if isinstance(failure, TimeoutError) else {}),
    }
    assert wire.adapter._api_post.await_count == 2
    assert wire.adapter._api_post.await_args_list[1].args[1]["root_id"] == "post-1"
    wire.adapter._api_get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("notify", [False, True])
async def test_cron_explicit_broken_root_never_posts_rootless(wire, notify):
    wire.adapter._last_post_status = 400
    wire.adapter._last_post_error = "invalid root_id"
    wire.adapter._api_post.side_effect = [{}, {"id": "flat-post", "root_id": ""}]
    result = await wire.adapter.send("channel-1", "Scheduled result", metadata={
        "cron_attach": True, "thread_id": "existing-root",
        "mattermost_explicit_thread": True, "notify": notify,
    })
    assert result.success is False
    assert wire.adapter._api_post.await_count == 1
    assert wire.adapter._api_post.await_args.args[1]["root_id"] == "existing-root"
    assert result.raw_response == {"message_ids": (), "source_confirmed_prefix": "", "source_attempted_prefix": ""}


@pytest.mark.asyncio
async def test_cron_chat_lookup_failure_does_not_invalidate_delivered_post(wire):
    wire.adapter._api_get.side_effect = TimeoutError("channel lookup failed")

    result = await wire.adapter.send("channel-1", "Scheduled result", metadata={"cron_attach": True})

    assert result.success is True
    assert result.message_id == "post-1"
    assert result.raw_response == {
        "message_ids": ("post-1",), "cron_root_id": "post-1",
        "source_confirmed_prefix": "Scheduled result", "source_attempted_prefix": "Scheduled result",
    }
    wire.adapter._api_get.assert_awaited_once_with("channels/channel-1")
    assert len(wire.posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_data", [{}, {"id":"channel-1"}, {"id":"other", "type":"D"}, {"id":"channel-1", "type":"unknown"}])
async def test_cron_does_not_claim_unverified_channel_type(wire, channel_data):
    wire.adapter._api_get.side_effect = None
    wire.adapter._api_get.return_value = channel_data
    result = await wire.adapter.send("channel-1", "Scheduled result", metadata={"cron_attach":True})
    assert result.success is True
    assert result.raw_response["cron_root_id"] == "post-1"
    assert "cron_chat_type" not in result.raw_response


@pytest.mark.asyncio
async def test_empty_cron_does_not_create_placeholder(wire):
    result = await wire.adapter.send("channel-1", "", metadata={"cron_attach": True})

    assert result.success is True
    assert result.message_id is None
    wire.adapter._api_post.assert_not_awaited()
    wire.adapter._api_get.assert_not_awaited()
