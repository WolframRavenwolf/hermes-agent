"""Tests for Mattermost platform adapter."""
import json
import os
import time
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from types import SimpleNamespace


@pytest.fixture
def mattermost_wire(monkeypatch):
    """Offline REST boundary shared by the preservation regressions."""
    wire = SimpleNamespace(posts=[], uploads=0, fail_at=None, raise_at=None)

    def post(url, **kwargs):
        response = AsyncMock()
        response.__aenter__.return_value = response
        response.text.return_value = "post failed"
        response.status = 201
        if url.endswith("/files"):
            wire.uploads += 1
            response.json.return_value = {"file_infos": [{"id": f"file-{wire.uploads}"}]}
        else:
            wire.posts.append(dict(kwargs["json"]))
            if len(wire.posts) == wire.raise_at:
                raise TimeoutError("ambiguous post timeout")
            if len(wire.posts) == wire.fail_at:
                response.status = 500
            response.json.return_value = {"id": f"post-{len(wire.posts)}"}
        return response

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post.side_effect = post
    monkeypatch.setattr("aiohttp.ClientSession", lambda **kwargs: session)
    return wire


@pytest.mark.asyncio
async def test_standalone_accepts_extract_media_tuple(mattermost_wire, tmp_path):
    from plugins.platforms.mattermost.adapter import _standalone_send
    path = tmp_path / "image.png"
    path.write_bytes(b"png")
    result = await _standalone_send(
        SimpleNamespace(token="test-token", extra={"url": "https://mm.example.com"}),
        "channel-1", "caption", thread_id="root-1",
        media_files=[(str(path), False)],
    )
    assert result["success"] is True
    assert mattermost_wire.posts[0]["file_ids"] == ["file-1"]
    assert mattermost_wire.posts[0]["root_id"] == "root-1"

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType
from gateway.run import (
    _resolve_gateway_display_bool,
    _resolve_progress_thread_id,
)


class TestMattermostProgressThreadRouting:
    def test_top_level_mattermost_progress_uses_event_message_id(self):
        assert _resolve_progress_thread_id(
            Platform.MATTERMOST,
            source_thread_id=None,
            event_message_id="top_post_123",
        ) == "top_post_123"


class TestMattermostDisplayHygiene:

    def test_mattermost_platform_opt_in_can_enable_interim_assistant_messages(self):
        """Mattermost can still opt into commentary explicitly per platform."""
        user_config = {
            "display": {
                "interim_assistant_messages": False,
                "platforms": {
                    "mattermost": {"interim_assistant_messages": True},
                },
            }
        }

        assert _resolve_gateway_display_bool(
            user_config,
            "mattermost",
            "interim_assistant_messages",
            default=True,
            platform=Platform.MATTERMOST,
            require_platform_override_for={Platform.MATTERMOST},
        ) is True


    def test_global_thinking_progress_still_applies_to_other_platforms(self):
        """The Mattermost guard must not silently neuter Telegram/other chats."""
        user_config = {"display": {"thinking_progress": True}}

        assert _resolve_gateway_display_bool(
            user_config,
            "telegram",
            "thinking_progress",
            default=False,
            platform=Platform.TELEGRAM,
            require_platform_override_for={Platform.MATTERMOST},
        ) is True


# ---------------------------------------------------------------------------
# Platform & Config
# ---------------------------------------------------------------------------

class TestMattermostConfigLoading:


    def test_mattermost_home_channel(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_TOKEN", "mm-tok-abc123")
        monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")
        monkeypatch.setenv("MATTERMOST_HOME_CHANNEL", "ch_abc123")
        monkeypatch.setenv("MATTERMOST_HOME_CHANNEL_NAME", "General")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        home = config.get_home_channel(Platform.MATTERMOST)
        assert home is not None
        assert home.chat_id == "ch_abc123"
        assert home.name == "General"


# ---------------------------------------------------------------------------
# Adapter format / truncate
# ---------------------------------------------------------------------------

def _make_adapter(extra=None):
    """Create a MattermostAdapter with mocked config."""
    from plugins.platforms.mattermost.adapter import MattermostAdapter
    adapter_extra = {"url": "https://mm.example.com"}
    if extra:
        adapter_extra.update(extra)
    config = PlatformConfig(
        enabled=True,
        token="test-token",
        extra=adapter_extra,
    )
    adapter = MattermostAdapter(config)
    return adapter


class TestMattermostFilePostMessage:
    def test_preserves_explicit_caption(self):
        from plugins.platforms.mattermost.adapter import _file_post_message

        assert _file_post_message(" report attached ", ["report.pdf"]) == "report attached"

    def test_uses_filename_for_file_only_post(self):
        from plugins.platforms.mattermost.adapter import _file_post_message

        assert _file_post_message("", ["voice.ogg"]) == "📎 voice.ogg"

    def test_lists_multiple_filenames(self):
        from plugins.platforms.mattermost.adapter import _file_post_message

        assert _file_post_message(None, ["one.png", "two.pdf"]) == (
            "📎 one.png\n📎 two.pdf"
        )


class TestMattermostFormatMessage:
    def setup_method(self):
        self.adapter = _make_adapter()

    def test_image_markdown_to_url(self):
        """![alt](url) should be converted to just the URL."""
        result = self.adapter.format_message("![cat](https://img.example.com/cat.png)")
        assert result == "https://img.example.com/cat.png"


    def test_regular_markdown_preserved(self):
        """Regular markdown (bold, italic, code) should be kept as-is."""
        content = "**bold** and *italic* and `code`"
        assert self.adapter.format_message(content) == content


class TestMattermostTruncateMessage:
    def setup_method(self):
        self.adapter = _make_adapter()


    def test_long_message_splits(self):
        msg = "a " * 2500  # 5000 chars
        chunks = self.adapter.truncate_message(msg, 4000)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= 4000

    def test_configured_max_post_length_is_exposed_to_streaming(self, monkeypatch):
        monkeypatch.delenv("MATTERMOST_MAX_POST_LENGTH", raising=False)
        adapter = _make_adapter({"max_post_length": 10_000})

        assert adapter.max_post_length == 10_000
        assert adapter.MAX_MESSAGE_LENGTH == 10_000

    def test_max_post_length_is_clamped_to_server_limit(self, monkeypatch):
        monkeypatch.delenv("MATTERMOST_MAX_POST_LENGTH", raising=False)
        adapter = _make_adapter({"max_post_length": 999_999})

        assert adapter.max_post_length == 16_383

    def test_tiny_max_post_length_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("MATTERMOST_MAX_POST_LENGTH", raising=False)
        adapter = _make_adapter({"max_post_length": 1})

        assert adapter.max_post_length == 4_000

    def test_env_max_post_length_overrides_config(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_MAX_POST_LENGTH", "12000")
        adapter = _make_adapter({"max_post_length": 10_000})

        assert adapter.max_post_length == 12_000

    def test_apply_yaml_config_keeps_profile_values_out_of_process_env(self, monkeypatch):
        env_names = (
            "MATTERMOST_MAX_POST_LENGTH",
            "MATTERMOST_REQUIRE_MENTION",
            "MATTERMOST_FREE_RESPONSE_CHANNELS",
            "MATTERMOST_ALLOWED_CHANNELS",
        )
        for name in env_names:
            monkeypatch.delenv(name, raising=False)
        from plugins.platforms.mattermost.adapter import _apply_yaml_config

        first = _apply_yaml_config(
            {},
            {
                "max_post_length": 500,
                "require_mention": True,
                "free_response_channels": ["first-free"],
                "allowed_channels": ["first-allowed"],
            },
        )
        second = _apply_yaml_config(
            {},
            {
                "max_post_length": 600,
                "require_mention": False,
                "free_response_channels": ["second-free"],
                "allowed_channels": ["second-allowed"],
            },
        )

        assert first == {
            "max_post_length": 500,
            "require_mention": True,
            "free_response_channels": ["first-free"],
            "allowed_channels": ["first-allowed"],
        }
        assert second == {
            "max_post_length": 600,
            "require_mention": False,
            "free_response_channels": ["second-free"],
            "allowed_channels": ["second-allowed"],
        }
        assert all(os.getenv(name) is None for name in env_names)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

class TestMattermostSend:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._session = MagicMock()

    @pytest.mark.asyncio
    async def test_send_calls_api_post(self):
        """send() should POST to /api/v4/posts with channel_id and message."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post123"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)

        result = await self.adapter.send("channel_1", "Hello!")

        assert result.success is True
        assert result.message_id == "post123"

        # Verify post was called with correct URL
        call_args = self.adapter._session.post.call_args
        assert "/api/v4/posts" in call_args[0][0]
        # Verify payload
        payload = call_args[1]["json"]
        assert payload["channel_id"] == "channel_1"
        assert payload["message"] == "Hello!"


    @pytest.mark.asyncio
    async def test_send_with_thread_reply(self):
        """When reply_mode is 'thread', reply_to should become root_id."""
        self.adapter._reply_mode = "thread"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post456"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        # send() now calls _resolve_root_id → _api_get("posts/<id>") first
        # to make sure root_id points to a thread root, so we need to mock
        # the GET too.  Return an empty dict (no root_id) so the resolver
        # falls back to the original reply_to as the root.
        mock_get_resp = AsyncMock()
        mock_get_resp.status = 200
        mock_get_resp.json = AsyncMock(return_value={"id": "root_post", "root_id": ""})
        mock_get_resp.text = AsyncMock(return_value="")
        mock_get_resp.__aenter__ = AsyncMock(return_value=mock_get_resp)
        mock_get_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)
        self.adapter._session.get = MagicMock(return_value=mock_get_resp)

        result = await self.adapter.send("channel_1", "Reply!", reply_to="root_post")

        assert result.success is True
        payload = self.adapter._session.post.call_args[1]["json"]
        assert payload["root_id"] == "root_post"


    @pytest.mark.asyncio
    async def test_progress_send_with_invalid_thread_root_never_falls_back_flat(self):
        """Tool/status/progress bubbles must stay quiet when the thread is broken."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "bad_root", "root_id": ""})
        self.adapter._last_post_status = 400
        self.adapter._last_post_error = "api.context.invalid_param.app_error: invalid root_id"
        self.adapter._api_post = AsyncMock(return_value={})

        result = await self.adapter.send(
            "channel_1",
            "⚙️ terminal...",
            metadata={"thread_id": "bad_root"},
        )

        assert result.success is False
        assert self.adapter._api_post.call_count == 1
        payload = self.adapter._api_post.call_args_list[0][0][1]
        assert payload["root_id"] == "bad_root"

    @pytest.mark.asyncio
    async def test_notify_send_with_invalid_thread_root_falls_back_flat_with_warning(self):
        """Notify-worthy replies may fall back flat so the answer is not lost."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "bad_root", "root_id": ""})
        self.adapter._last_post_status = 400
        self.adapter._last_post_error = "api.context.invalid_param.app_error: invalid root_id"
        self.adapter._api_post = AsyncMock(side_effect=[{}, {"id": "flat_final"}])

        result = await self.adapter.send(
            "channel_1",
            "Final answer body",
            reply_to="bad_root",
            metadata={"notify": True},
        )

        assert result.success is True
        assert result.message_id == "flat_final"
        assert self.adapter._api_post.call_count == 2
        threaded_payload = self.adapter._api_post.call_args_list[0][0][1]
        flat_payload = self.adapter._api_post.call_args_list[1][0][1]
        assert threaded_payload["root_id"] == "bad_root"
        assert "root_id" not in flat_payload
        assert flat_payload["channel_id"] == "channel_1"
        assert "Mattermost thread delivery failed" in flat_payload["message"]
        assert "Final answer body" in flat_payload["message"]

    @pytest.mark.asyncio
    async def test_thread_fallback_prefix_respects_configured_post_hard_cap(
        self, monkeypatch
    ):
        monkeypatch.delenv("MATTERMOST_MAX_POST_LENGTH", raising=False)
        adapter = _make_adapter({"max_post_length": 500, "reply_mode": "thread"})
        adapter._api_get = AsyncMock(
            return_value={"id": "bad_root", "root_id": ""}
        )
        adapter._last_post_status = 400
        adapter._last_post_error = "invalid root_id"
        payloads = []

        async def _post(_path, payload):
            payloads.append(dict(payload))
            if "root_id" in payload:
                return {}
            return {"id": f"flat-{len(payloads)}"}

        adapter._api_post = AsyncMock(side_effect=_post)
        body = "x" * 500

        result = await adapter.send(
            "channel_1",
            body,
            reply_to="bad_root",
            metadata={"notify": True},
        )

        flat_payloads = [payload for payload in payloads if "root_id" not in payload]
        assert result.success is True
        assert result.message_id == "flat-2"
        assert result.continuation_message_ids == ()
        assert result.raw_response == {
            "message_ids": ("flat-2",),
            "source_confirmed_prefix": body,
            "source_attempted_prefix": body,
        }
        assert len(payloads) == 2
        assert len(flat_payloads) == 1
        assert flat_payloads[0]["message"] == body
        assert len(flat_payloads[0]["message"]) == 500


    @pytest.mark.asyncio
    async def test_progress_send_with_broken_thread_and_no_recorded_error_stays_quiet(self):
        """Same rule when no post error was recorded: still no flat fallback."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "bad_root", "root_id": ""})
        self.adapter._api_post = AsyncMock(return_value={})

        result = await self.adapter.send(
            "channel_1",
            "⚙️ terminal...",
            metadata={"thread_id": "bad_root"},
        )

        assert result.success is False
        assert self.adapter._api_post.call_count == 1
        payload = self.adapter._api_post.call_args_list[0][0][1]
        assert payload["root_id"] == "bad_root"


# ---------------------------------------------------------------------------
# WebSocket event parsing
# ---------------------------------------------------------------------------

class TestMattermostWebSocketParsing:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        self.adapter._bot_username = "hermes-bot"
        # Mock handle_message to capture the MessageEvent without processing
        self.adapter.handle_message = AsyncMock()

    @pytest.mark.asyncio
    async def test_parse_posted_event(self):
        """'posted' events should extract message from double-encoded post JSON."""
        post_data = {
            "id": "post_abc",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "@bot_user_id Hello from Matrix!",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),  # double-encoded JSON string
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        # @mention is stripped from the message text
        assert msg_event.text == "Hello from Matrix!"
        assert msg_event.message_id == "post_abc"


    @pytest.mark.asyncio
    async def test_ignore_system_posts(self):
        """Posts with a 'type' field (system messages) should be ignored."""
        post_data = {
            "id": "sys_post",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "user joined",
            "type": "system_join_channel",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert not self.adapter.handle_message.called


    @pytest.mark.asyncio
    async def test_leading_space_slash_command_is_command(self):
        """Mattermost mobile suggests leading-space slash commands."""
        post_data = {
            "id": "post_cmd",
            "user_id": "user_123",
            "channel_id": "chan_dm",
            "message": " /new",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "D",
                "sender_name": "@bob",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "/new"
        assert msg_event.message_type is MessageType.COMMAND
        assert msg_event.get_command() == "new"


# ---------------------------------------------------------------------------
# Mention behavior (require_mention + free_response_channels)
# ---------------------------------------------------------------------------

class TestMattermostMentionBehavior:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        self.adapter._bot_username = "hermes-bot"
        self.adapter.handle_message = AsyncMock()

    def _make_event(self, message, channel_type="O", channel_id="chan_456"):
        post_data = {
            "id": "post_mention",
            "user_id": "user_123",
            "channel_id": channel_id,
            "message": message,
        }
        return {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": channel_type,
                "sender_name": "@alice",
            },
        }

    @pytest.mark.asyncio
    async def test_require_mention_true_skips_without_mention(self):
        """Default: messages without @mention in channels are skipped."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            os.environ.pop("MATTERMOST_FREE_RESPONSE_CHANNELS", None)
            await self.adapter._handle_ws_event(self._make_event("hello"))
            assert not self.adapter.handle_message.called


    @pytest.mark.asyncio
    async def test_free_response_channel_responds_without_mention(self):
        """Messages in free-response channels don't need @mention."""
        with patch.dict(os.environ, {"MATTERMOST_FREE_RESPONSE_CHANNELS": "chan_456,chan_789"}):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            await self.adapter._handle_ws_event(self._make_event("hello", channel_id="chan_456"))
            assert self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_operator_env_allowed_channels_overrides_yaml_extra(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_ALLOWED_CHANNELS", "operator-channel")
        adapter = _make_adapter({"allowed_channels": ["yaml-channel"]})
        adapter._bot_user_id = "bot_user_id"
        adapter._bot_username = "hermes-bot"
        adapter.handle_message = AsyncMock()

        await adapter._handle_ws_event(
            self._make_event(
                "@hermes-bot hello",
                channel_id="operator-channel",
            )
        )

        adapter.handle_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# File upload (send_image)
# ---------------------------------------------------------------------------

class TestMattermostFileUpload:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._session = MagicMock()

    @pytest.mark.asyncio
    @patch("tools.url_safety.is_safe_url", return_value=True)
    async def test_send_image_downloads_and_uploads(self, _mock_safe):
        """send_image should download the URL, upload via /api/v4/files, then post."""
        # Mock the download (GET)
        mock_dl_resp = AsyncMock()
        mock_dl_resp.status = 200
        mock_dl_resp.read = AsyncMock(return_value=b"\x89PNG\x00fake-image-data")
        mock_dl_resp.content_type = "image/png"
        mock_dl_resp.__aenter__ = AsyncMock(return_value=mock_dl_resp)
        mock_dl_resp.__aexit__ = AsyncMock(return_value=False)

        # Mock the upload (POST to /files)
        mock_upload_resp = AsyncMock()
        mock_upload_resp.status = 200
        mock_upload_resp.json = AsyncMock(return_value={
            "file_infos": [{"id": "file_abc123"}]
        })
        mock_upload_resp.text = AsyncMock(return_value="")
        mock_upload_resp.__aenter__ = AsyncMock(return_value=mock_upload_resp)
        mock_upload_resp.__aexit__ = AsyncMock(return_value=False)

        # Mock the post (POST to /posts)
        mock_post_resp = AsyncMock()
        mock_post_resp.status = 200
        mock_post_resp.json = AsyncMock(return_value={"id": "post_with_file"})
        mock_post_resp.text = AsyncMock(return_value="")
        mock_post_resp.__aenter__ = AsyncMock(return_value=mock_post_resp)
        mock_post_resp.__aexit__ = AsyncMock(return_value=False)

        # Route calls: first GET (download), then POST (upload), then POST (create post)
        self.adapter._session.get = MagicMock(return_value=mock_dl_resp)
        post_call_count = 0
        original_post_returns = [mock_upload_resp, mock_post_resp]

        def post_side_effect(*args, **kwargs):
            nonlocal post_call_count
            resp = original_post_returns[min(post_call_count, len(original_post_returns) - 1)]
            post_call_count += 1
            return resp

        self.adapter._session.post = MagicMock(side_effect=post_side_effect)

        result = await self.adapter.send_image(
            "channel_1", "https://img.example.com/cat.png", caption="A cat"
        )

        assert result.success is True
        assert result.message_id == "post_with_file"

    @pytest.mark.asyncio
    async def test_send_image_file_adds_filename_caption_and_keeps_thread(self, tmp_path):
        self.adapter._reply_mode = "thread"
        image_path = tmp_path / "example.png"
        image_path.write_bytes(b"png")
        self.adapter._upload_file = AsyncMock(return_value="file_123")
        self.adapter._api_get = AsyncMock(
            return_value={"id": "root_post_123", "root_id": ""}
        )
        self.adapter._api_post = AsyncMock(return_value={"id": "post_with_file"})

        result = await self.adapter.send_image_file(
            "channel_1",
            str(image_path),
            metadata={"thread_id": "root_post_123"},
        )

        assert result.success is True
        payload = self.adapter._api_post.call_args.args[1]
        assert payload["root_id"] == "root_post_123"
        assert payload["file_ids"] == ["file_123"]
        assert payload["message"] == "📎 example.png"

    @pytest.mark.asyncio
    async def test_send_multiple_images_adds_filename_caption_and_keeps_thread(
        self, tmp_path
    ):
        self.adapter._reply_mode = "thread"
        image_path = tmp_path / "example.png"
        image_path.write_bytes(b"png")
        self.adapter._upload_file = AsyncMock(return_value="file_123")
        self.adapter._api_get = AsyncMock(
            return_value={"id": "root_post_123", "root_id": ""}
        )
        self.adapter._api_post = AsyncMock(return_value={"id": "post_with_file"})

        await self.adapter.send_multiple_images(
            "channel_1",
            [(f"file://{image_path}", "")],
            metadata={"thread_id": "root_post_123"},
        )

        payload = self.adapter._api_post.call_args.args[1]
        assert payload["root_id"] == "root_post_123"
        assert payload["file_ids"] == ["file_123"]
        assert payload["message"] == "📎 example.png"


# ---------------------------------------------------------------------------
# Dedup cache
# ---------------------------------------------------------------------------

class TestMattermostDedup:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        # Mock handle_message to capture calls without processing
        self.adapter.handle_message = AsyncMock()


    def test_prune_seen_clears_expired(self):
        """Dedup cache should remove entries older than TTL on overflow."""
        now = time.time()
        dedup = self.adapter._dedup
        # Fill with enough expired entries to trigger pruning
        for i in range(dedup._max_size + 10):
            dedup._seen[f"old_{i}"] = now - 600  # 10 min ago (older than default TTL)

        # Add a fresh one
        dedup._seen["fresh"] = now

        # Trigger pruning by calling is_duplicate with a new entry (over max_size)
        dedup.is_duplicate("trigger_prune")

        # Old entries should be pruned, fresh one kept
        assert "fresh" in dedup._seen
        assert len(dedup._seen) < dedup._max_size + 10


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------

class TestMattermostRequirements:
    def test_check_requirements_with_token_and_url(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_TOKEN", "test-token")
        monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")
        from plugins.platforms.mattermost.adapter import check_mattermost_requirements
        assert check_mattermost_requirements() is True


    def test_validate_config_accepts_platform_values(self, monkeypatch):
        monkeypatch.delenv("MATTERMOST_TOKEN", raising=False)
        monkeypatch.delenv("MATTERMOST_URL", raising=False)
        from plugins.platforms.mattermost.adapter import validate_mattermost_config

        config = PlatformConfig(
            enabled=True,
            token="cfg-token",
            extra={"url": "https://mm.example.com"},
        )
        assert validate_mattermost_config(config) is True


# ---------------------------------------------------------------------------
# Media type propagation (MIME types, not bare strings)
# ---------------------------------------------------------------------------

class TestMattermostMediaTypes:
    """Verify that media_types contains actual MIME types (e.g. 'image/png')
    rather than bare category strings ('image'), so downstream
    ``mtype.startswith("image/")`` checks in run.py work correctly."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        self.adapter.handle_message = AsyncMock()

    def _make_event(self, file_ids):
        post_data = {
            "id": "post_media",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "@bot_user_id file attached",
            "file_ids": file_ids,
        }
        return {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

    @pytest.mark.asyncio
    async def test_image_media_type_is_full_mime(self):
        """An image attachment should produce 'image/png', not 'image'."""
        file_info = {"name": "photo.png", "mime_type": "image/png"}
        self.adapter._api_get = AsyncMock(return_value=file_info)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"\x89PNG fake")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        self.adapter._session = MagicMock()
        self.adapter._session.get = MagicMock(return_value=mock_resp)

        with patch("gateway.platforms.base.cache_image_from_bytes", return_value="/tmp/photo.png"):
            await self.adapter._handle_ws_event(self._make_event(["file1"]))

        msg = self.adapter.handle_message.call_args[0][0]
        assert msg.media_types == ["image/png"]
        assert msg.media_types[0].startswith("image/")


@pytest.mark.asyncio
async def test_mattermost_top_level_channel_post_is_thread_root():
    adapter = _make_adapter()
    adapter._reply_mode = "thread"
    adapter._bot_user_id = "bot_user_id"
    adapter._bot_username = "hermes-bot"
    adapter.handle_message = AsyncMock()
    post_data = {
        "id": "top_post_123",
        "user_id": "user_123",
        "channel_id": "chan_456",
        "message": "@hermes-bot start work",
        "root_id": "",
    }
    event = {
        "event": "posted",
        "data": {
            "post": json.dumps(post_data),
            "channel_type": "O",
            "sender_name": "@alice",
        },
    }

    await adapter._handle_ws_event(event)

    msg_event = adapter.handle_message.call_args[0][0]
    assert msg_event.source.thread_id == "top_post_123"
    assert msg_event.source.message_id == "top_post_123"
    assert msg_event.message_id == "top_post_123"




class TestMattermostPortLimits:
    @pytest.mark.asyncio
    async def test_edit_chunks_losslessly_and_surfaces_every_continuation(
        self, monkeypatch
    ):
        monkeypatch.delenv("MATTERMOST_MAX_POST_LENGTH", raising=False)
        adapter = _make_adapter({"max_post_length": 500})
        adapter._reply_mode = "off"
        put_payloads = []
        post_payloads = []

        async def fake_put(_path, payload):
            put_payloads.append(dict(payload))
            return {"id": "original"}

        async def fake_post(_path, payload):
            post_payloads.append(dict(payload))
            return {"id": f"continuation-{len(post_payloads)}"}

        adapter._api_put = AsyncMock(side_effect=fake_put)
        adapter._api_post = AsyncMock(side_effect=fake_post)

        result = await adapter.edit_message(
            "channel_1", "original", "x" * 1200, finalize=True
        )

        payloads = put_payloads + post_payloads
        assert result.success is True
        assert result.message_id == "continuation-2"
        assert result.continuation_message_ids == (
            "continuation-1",
            "continuation-2",
        )
        assert all(len(payload["message"]) <= 500 for payload in payloads)
        assert sum(payload["message"].count("x") for payload in payloads) == 1200

    @pytest.mark.asyncio
    async def test_standalone_media_cap_plus_one_batches_without_repeating_caption(
        self, monkeypatch, tmp_path
    ):
        from plugins.platforms.mattermost.adapter import _standalone_send

        media_paths = []
        for index in range(6):
            path = tmp_path / f"asset-{index}.bin"
            path.write_bytes(str(index).encode())
            media_paths.append(path)
        post_payloads = []
        upload_count = 0

        class FakeResponse:
            def __init__(self, status, data):
                self.status = status
                self._data = data

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return self._data

            async def text(self):
                return ""

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, url, **kwargs):
                nonlocal upload_count
                if url.endswith("/files"):
                    upload_count += 1
                    return FakeResponse(
                        201, {"file_infos": [{"id": f"file-{upload_count}"}]}
                    )
                post_payloads.append(dict(kwargs["json"]))
                return FakeResponse(201, {"id": f"post-{len(post_payloads)}"})

        monkeypatch.setattr("aiohttp.ClientSession", lambda **_kwargs: FakeSession())
        caption = "cap+1 attachment caption"

        result = await _standalone_send(
            SimpleNamespace(
                token="test-token",
                extra={"url": "https://mm.example.com"},
            ),
            "channel_1",
            caption,
            thread_id="root-1",
            media_files=[str(path) for path in media_paths],
        )

        assert result == {
            "success": True,
            "platform": "mattermost",
            "chat_id": "channel_1",
            "message_id": "post-2",
            "message_ids": ["post-1", "post-2"],
        }
        assert [payload["file_ids"] for payload in post_payloads] == [
            ["file-1", "file-2", "file-3", "file-4", "file-5"],
            ["file-6"],
        ]
        assert all(len(payload["file_ids"]) <= 5 for payload in post_payloads)
        assert len({fid for payload in post_payloads for fid in payload["file_ids"]}) == 6
        assert [payload["root_id"] for payload in post_payloads] == ["root-1", "root-1"]
        assert post_payloads[0]["message"] == caption
        assert caption not in post_payloads[1]["message"]

    @pytest.mark.asyncio
    async def test_standalone_missing_media_is_truthful_partial_failure(
        self, monkeypatch, tmp_path
    ):
        from plugins.platforms.mattermost.adapter import _standalone_send

        good = tmp_path / "good.bin"
        missing = tmp_path / "missing.bin"
        good.write_bytes(b"good")

        class FakeResponse:
            status = 201

            def __init__(self, data):
                self._data = data

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return self._data

            async def text(self):
                return ""

        provider_posts = []

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, url, **_kwargs):
                if url.endswith("/files"):
                    return FakeResponse({"file_infos": [{"id": "file-1"}]})
                provider_posts.append(url)
                return FakeResponse({"id": "post-1"})

        monkeypatch.setattr("aiohttp.ClientSession", lambda **_kwargs: FakeSession())
        result = await _standalone_send(
            SimpleNamespace(token="token", extra={"url": "https://mm.example.com"}),
            "channel-1",
            "caption",
            media_files=[str(good), str(missing)],
        )

        assert result["success"] is False
        assert result["media_delivered"] is False
        assert result["partial_failure"] is True
        assert result["message_ids"] == ["post-1"]
        assert result["error"] == "Not all requested Mattermost media were accepted for delivery"
        assert str(missing) not in json.dumps(result)

        post_count = len(provider_posts)
        rejected = await _standalone_send(
            SimpleNamespace(token="token", extra={"url": "https://mm.example.com"}),
            "channel-1",
            "",
            media_files=[str(missing)],
        )
        assert len(provider_posts) == post_count
        assert rejected["success"] is False
        assert rejected["media_delivered"] is False
        assert rejected["partial_failure"] is False
        assert rejected["message_ids"] == []
        assert str(missing) not in json.dumps(rejected)

        import builtins
        real_open = builtins.open

        def racing_open(path, *args, **kwargs):
            if str(path) == str(good):
                raise FileNotFoundError(str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", racing_open)
        raced = await _standalone_send(
            SimpleNamespace(token="token", extra={"url": "https://mm.example.com"}),
            "channel-1",
            "caption",
            media_files=[str(good)],
        )
        assert len(provider_posts) == post_count
        assert raced["success"] is False
        assert raced["media_delivered"] is False
        assert raced["partial_failure"] is False
        assert raced["message_ids"] == []
        assert str(good) not in json.dumps(raced)

    @pytest.mark.asyncio
    async def test_standalone_media_partial_failure_returns_all_post_ids_and_stops_uploads(
        self, monkeypatch, tmp_path
    ):
        from plugins.platforms.mattermost.adapter import _standalone_send

        media_paths = []
        for index in range(11):
            path = tmp_path / f"asset-{index}.bin"
            path.write_bytes(str(index).encode())
            media_paths.append(path)
        post_payloads = []
        upload_count = 0

        class FakeResponse:
            def __init__(self, status, data, body=""):
                self.status = status
                self._data = data
                self._body = body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return self._data

            async def text(self):
                return self._body

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, url, **kwargs):
                nonlocal upload_count
                if url.endswith("/files"):
                    upload_count += 1
                    return FakeResponse(
                        201, {"file_infos": [{"id": f"file-{upload_count}"}]}
                    )
                post_payloads.append(dict(kwargs["json"]))
                if len(post_payloads) == 2:
                    return FakeResponse(500, {}, "post failed")
                return FakeResponse(201, {"id": f"post-{len(post_payloads)}"})

        monkeypatch.setattr("aiohttp.ClientSession", lambda **_kwargs: FakeSession())

        result = await _standalone_send(
            SimpleNamespace(
                token="test-token",
                extra={"url": "https://mm.example.com"},
            ),
            "channel_1",
            "caption delivered once",
            media_files=[str(path) for path in media_paths],
        )

        assert result["success"] is False
        assert result["partial_failure"] is True
        assert result["message_id"] == "post-1"
        assert result["message_ids"] == ["post-1"]
        assert "Mattermost API error (500)" in result["error"]
        assert upload_count == 10
        assert len(post_payloads) == 2
        assert all(len(payload["file_ids"]) == 5 for payload in post_payloads)
        assert set(post_payloads[0]["file_ids"]).isdisjoint(
            post_payloads[1]["file_ids"]
        )
        assert post_payloads[0]["message"] == "caption delivered once"
        assert "caption delivered once" not in post_payloads[1]["message"]

    @pytest.mark.asyncio
    async def test_standalone_success_without_post_id_is_explicit_failure(
        self, monkeypatch
    ):
        from plugins.platforms.mattermost.adapter import _standalone_send

        class FakeResponse:
            status = 201

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return {}

            async def text(self):
                return ""

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, _url, **_kwargs):
                return FakeResponse()

        monkeypatch.setattr("aiohttp.ClientSession", lambda **_kwargs: FakeSession())

        result = await _standalone_send(
            SimpleNamespace(
                token="test-token",
                extra={"url": "https://mm.example.com"},
            ),
            "channel_1",
            "hello",
        )

        assert result["success"] is False
        assert result["partial_failure"] is False
        assert result["message_id"] is None
        assert result["message_ids"] == []
        assert "missing post id" in result["error"].lower()


class TestMattermostPortCaptions:
    @pytest.mark.asyncio
    async def test_local_file_caption_is_chunked_and_attached_once(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("MATTERMOST_MAX_POST_LENGTH", raising=False)
        adapter = _make_adapter({"max_post_length": 500})
        image_path = tmp_path / "example.png"
        image_path.write_bytes(b"png")
        adapter._upload_file = AsyncMock(return_value="file_123")
        payloads = []

        async def fake_post(_path, payload):
            payloads.append(dict(payload))
            return {"id": f"post-{len(payloads)}"}

        adapter._api_post = AsyncMock(side_effect=fake_post)

        result = await adapter.send_image_file(
            "channel_1", str(image_path), caption="x" * 1200
        )

        assert result.success is True
        assert all(len(payload["message"]) <= 500 for payload in payloads)
        assert sum(payload["message"].count("x") for payload in payloads) == 1200
        assert [payload.get("file_ids") for payload in payloads] == [
            ["file_123"],
            None,
            None,
        ]

    @pytest.mark.asyncio
    @patch("tools.url_safety.is_safe_url", return_value=True)
    async def test_url_media_caption_is_chunked_and_attached_once(
        self, _mock_safe, monkeypatch
    ):
        monkeypatch.delenv("MATTERMOST_MAX_POST_LENGTH", raising=False)
        adapter = _make_adapter({"max_post_length": 500})
        response = AsyncMock()
        response.status = 200
        response.read = AsyncMock(return_value=b"png")
        response.content_type = "image/png"
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        adapter._session = MagicMock()
        adapter._session.get = MagicMock(return_value=response)
        adapter._upload_file = AsyncMock(return_value="file_123")
        payloads = []

        async def fake_post(_path, payload):
            payloads.append(dict(payload))
            return {"id": f"post-{len(payloads)}"}

        adapter._api_post = AsyncMock(side_effect=fake_post)

        result = await adapter.send_image(
            "channel_1", "https://img.example.com/example.png", caption="x" * 1200
        )

        assert result.success is True
        assert all(len(payload["message"]) <= 500 for payload in payloads)
        assert sum(payload["message"].count("x") for payload in payloads) == 1200
        assert [payload.get("file_ids") for payload in payloads] == [
            ["file_123"],
            None,
            None,
        ]

    @pytest.mark.asyncio
    async def test_multiple_image_caption_is_chunked_and_attached_once(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("MATTERMOST_MAX_POST_LENGTH", raising=False)
        adapter = _make_adapter({"max_post_length": 500})
        image_path = tmp_path / "example.png"
        image_path.write_bytes(b"png")
        adapter._upload_file = AsyncMock(return_value="file_123")
        payloads = []

        async def fake_post(_path, payload):
            payloads.append(dict(payload))
            return {"id": f"post-{len(payloads)}"}

        adapter._api_post = AsyncMock(side_effect=fake_post)

        await adapter.send_multiple_images(
            "channel_1",
            [(f"file://{image_path}", "x" * 1200)],
        )

        assert all(len(payload["message"]) <= 500 for payload in payloads)
        assert sum(payload["message"].count("x") for payload in payloads) == 1200
        assert [payload.get("file_ids") for payload in payloads] == [
            ["file_123"],
            None,
            None,
        ]

    @pytest.mark.asyncio
    async def test_standalone_file_caption_is_chunked_and_attached_once(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("MATTERMOST_MAX_POST_LENGTH", raising=False)
        from plugins.platforms.mattermost.adapter import _standalone_send

        media_path = tmp_path / "report.pdf"
        media_path.write_bytes(b"pdf")
        post_payloads = []

        class FakeResponse:
            def __init__(self, status, data):
                self.status = status
                self._data = data

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return self._data

            async def text(self):
                return ""

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, url, **kwargs):
                if url.endswith("/files"):
                    return FakeResponse(201, {"file_infos": [{"id": "file_123"}]})
                post_payloads.append(dict(kwargs["json"]))
                return FakeResponse(201, {"id": f"post-{len(post_payloads)}"})

        monkeypatch.setattr("aiohttp.ClientSession", lambda **_kwargs: FakeSession())
        result = await _standalone_send(
            SimpleNamespace(
                token="test-token",
                extra={
                    "url": "https://mm.example.com",
                    "max_post_length": 500,
                },
            ),
            "channel_1",
            "x" * 1200,
            media_files=[str(media_path)],
        )

        assert result["success"] is True
        assert all(len(payload["message"]) <= 500 for payload in post_payloads)
        assert sum(payload["message"].count("x") for payload in post_payloads) == 1200
        assert [payload.get("file_ids") for payload in post_payloads] == [
            ["file_123"],
            None,
            None,
        ]

    @pytest.mark.asyncio
    async def test_public_media_send_preserves_all_ids_on_long_text_partial_failure(
        self, monkeypatch, tmp_path
    ):
        from tools.send_message_tool import _send_to_platform

        monkeypatch.delenv("MATTERMOST_MAX_POST_LENGTH", raising=False)
        media_path = tmp_path / "asset.bin"
        media_path.write_bytes(b"media")
        post_payloads = []
        upload_count = 0

        class FakeResponse:
            def __init__(self, status, data, body=""):
                self.status = status
                self._data = data
                self._body = body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return self._data

            async def text(self):
                return self._body

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, url, **kwargs):
                nonlocal upload_count
                if url.endswith("/files"):
                    upload_count += 1
                    return FakeResponse(201, {"file_infos": [{"id": "file-1"}]})
                post_payloads.append(dict(kwargs["json"]))
                if len(post_payloads) == 3:
                    return FakeResponse(500, {}, "post failed")
                return FakeResponse(201, {"id": f"post-{len(post_payloads)}"})

        monkeypatch.setattr("aiohttp.ClientSession", lambda **_kwargs: FakeSession())

        result = await _send_to_platform(
            Platform.MATTERMOST,
            PlatformConfig(
                enabled=True,
                token="test-token",
                extra={"url": "https://mm.example.com", "max_post_length": 500},
            ),
            "channel_1",
            "x" * 1200,
            media_files=[str(media_path)],
        )

        assert result["success"] is False
        assert result["partial_failure"] is True
        assert result["message_id"] == "post-2"
        assert result["message_ids"] == ["post-1", "post-2"]
        assert "Mattermost API error (500)" in result["error"]
        assert upload_count == 1
        assert len(post_payloads) == 3
        assert sum(payload["message"].count("x") for payload in post_payloads) == 1200
        assert [payload.get("file_ids") for payload in post_payloads] == [
            ["file-1"],
            None,
            None,
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_live_public_media_caption_receipts_and_thread(monkeypatch, tmp_path, failure):
    import sys
    from tools.send_message_tool import _send_to_platform
    adapter = _make_adapter({"max_post_length": 500})
    # Explicit tool threads must survive the default in-channel reply mode.
    adapter._api_get = AsyncMock(return_value={"id": "root-1", "root_id": ""})
    adapter._upload_file = AsyncMock(side_effect=[f"file-{i}" for i in range(6)])
    payloads = []
    async def post(_path, payload):
        payloads.append(dict(payload))
        if failure and len(payloads) == 3:
            return {}
        return {"id": f"post-{len(payloads)}"}
    adapter._api_post = AsyncMock(side_effect=post)
    runner = SimpleNamespace(adapters={Platform.MATTERMOST: adapter})
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_gateway_runner_ref=lambda: runner))
    media = []
    for i in range(6):
        path = tmp_path / f"asset-{i}.png"
        path.write_bytes(b"png")
        media.append((str(path), False))
    result = await _send_to_platform(
        Platform.MATTERMOST, adapter.config, "channel-1", "x" * 1200,
        thread_id="root-1", media_files=media,
    )
    assert result["success"] is not failure
    assert result["message_ids"] == (["post-1", "post-2"] if failure else ["post-1", "post-2", "post-3", "post-4"])
    assert all(len(p["message"]) <= 500 for p in payloads)
    assert all(p.get("root_id") == "root-1" for p in payloads)
    assert sum(p["message"].count("x") for p in payloads) == 1200
    assert payloads[0]["file_ids"] == [f"file-{i}" for i in range(5)]
    assert not payloads[1].get("file_ids")
    if failure:
        assert result["partial_failure"] is True
        assert adapter._upload_file.await_count == 5
    else:
        assert payloads[-1]["file_ids"] == ["file-5"]


@pytest.mark.asyncio
async def test_live_public_missing_only_media_fails_without_post(monkeypatch, tmp_path):
    import sys
    from tools.send_message_tool import _send_to_platform

    adapter = _make_adapter()
    adapter._api_post = AsyncMock(side_effect=AssertionError("provider post must not run"))
    missing = tmp_path / "missing.png"
    runner = SimpleNamespace(adapters={Platform.MATTERMOST: adapter})
    monkeypatch.setitem(
        sys.modules,
        "gateway.run",
        SimpleNamespace(_gateway_runner_ref=lambda: runner),
    )

    result = await _send_to_platform(
        Platform.MATTERMOST,
        adapter.config,
        "channel-1",
        "",
        media_files=[(str(missing), False)],
    )

    assert result["success"] is False
    assert result["media_delivered"] is False
    assert result["partial_failure"] is False
    assert result["message_ids"] == []
    assert str(missing) not in json.dumps(result)
    adapter._api_post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["send", "edit_message", "send_image_file"])
async def test_partial_exception_retains_acknowledged_ids(monkeypatch, tmp_path, operation):
    adapter = _make_adapter({"max_post_length": 500})
    adapter._api_put = AsyncMock(return_value={"id": "original"})
    adapter._api_post = AsyncMock(side_effect=[{"id": "post-1"}, TimeoutError("ambiguous")])
    adapter._upload_file = AsyncMock(return_value="file-1")
    if operation == "edit_message":
        result = await adapter.edit_message("channel-1", "original", "x" * 1700)
    elif operation == "send_image_file":
        path = tmp_path / "image.png"
        path.write_bytes(b"png")
        result = await adapter.send_image_file("channel-1", str(path), caption="x" * 1200)
    else:
        result = await adapter.send("channel-1", "x" * 1200)
    assert result.success is False
    assert result.message_id == "post-1"
    assert "post-1" in result.continuation_message_ids
    assert adapter._api_post.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_file", [False, True])
async def test_live_public_caption_survives_missing_or_failed_first_upload(monkeypatch, tmp_path, existing_file):
    import sys
    from tools.send_message_tool import _send_to_platform
    adapter = _make_adapter({"max_post_length": 500})
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    media = [(str(first), False)]
    if existing_file:
        first.write_bytes(b"png")
        second.write_bytes(b"png")
        media.append((str(second), False))
    adapter._upload_file = AsyncMock(side_effect=[None, "file-2"])
    adapter._api_post = AsyncMock(return_value={"id": "post-1"})
    runner = SimpleNamespace(adapters={Platform.MATTERMOST: adapter})
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_gateway_runner_ref=lambda: runner))
    result = await _send_to_platform(
        Platform.MATTERMOST, adapter.config, "channel-1", "complete caption", media_files=media,
    )
    assert result["success"] is False
    assert result["media_delivered"] is False
    assert result["partial_failure"] is True
    assert result["message_ids"] == ["post-1"]
    assert adapter._api_post.call_args.args[1]["message"] == "complete caption"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, 400])
@pytest.mark.parametrize("public_send", [False, True])
async def test_batch_fallback_requires_authoritative_rejection(
    monkeypatch, tmp_path, status, public_send
):
    adapter = _make_adapter()
    if not public_send:
        adapter._reply_mode = "thread"
    # Exercise URI decoding and actual local reads, not mocked media dispatch.
    path = tmp_path / "image ü #25%.png"
    path.write_bytes(b"local image bytes")
    adapter._upload_file = AsyncMock(side_effect=["file-1", "file-2"])
    adapter._api_get = AsyncMock(return_value={"id": "root-1", "root_id": ""})
    adapter._last_post_status = status
    adapter._api_post = AsyncMock(side_effect=[{}, {"id": "fallback-1"}])
    if public_send:
        import sys
        from tools.send_message_tool import _send_to_platform

        runner = SimpleNamespace(adapters={Platform.MATTERMOST: adapter})
        monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_gateway_runner_ref=lambda: runner))
        result = await _send_to_platform(
            Platform.MATTERMOST, adapter.config, "channel-1", "caption",
            thread_id="root-1", media_files=[(str(path), False)],
        )
        assert result["success"] is (status == 400)
        if status == 400:
            assert result["message_ids"] == ["fallback-1"]
            assert result["media_delivered"] is True
    else:
        result = await adapter.send_multiple_images(
            "channel-1", [(path.as_uri(), "caption")], metadata={"thread_id": "root-1"},
        )
        assert result.success is (status == 400)
        if status == 400:
            assert result.raw_response["message_ids"] == ("fallback-1",)
    payloads = [call.args[1] for call in adapter._api_post.await_args_list]
    assert len(payloads) == (2 if status == 400 else 1)
    assert [payload.get("file_ids") for payload in payloads] == (
        [["file-1"], ["file-2"]] if status == 400 else [["file-1"]]
    )
    assert all(payload["message"] == "caption" for payload in payloads)
    assert all("file://" not in payload["message"] for payload in payloads)
    assert all(payload["root_id"] == "root-1" for payload in payloads)
    assert adapter._upload_file.await_count == len(payloads)
    for call in adapter._upload_file.await_args_list:
        assert call.args == ("channel-1", b"local image bytes", path.name, "image/png")


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_file", [False, True])
@pytest.mark.parametrize("shared_caption", [None, "shared override", ""])
async def test_descriptor_captions_survive_missing_or_failed_first_upload(
    tmp_path, existing_file, shared_caption
):
    adapter = _make_adapter()
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    if existing_file:
        first.write_bytes(b"first")
    second.write_bytes(b"second")
    adapter._upload_file = AsyncMock(
        side_effect=[None, "file-2"] if existing_file else ["file-2"],
    )
    adapter._api_post = AsyncMock(return_value={"id": "post-1"})
    # Omit the keyword entirely for the original descriptor-only API.
    kwargs = {} if shared_caption is None else {"caption": shared_caption}
    result = await adapter.send_multiple_images(
        "channel-1", [(first.as_uri(), "first caption"), (second.as_uri(), "second caption")],
        **kwargs,
    )
    assert result.success is True
    assert result.raw_response["message_ids"] == ("post-1",)
    adapter._api_post.assert_awaited_once()
    payload = adapter._api_post.await_args_list[0].args[1]
    assert payload["file_ids"] == ["file-2"]
    assert payload["message"] == (
        "first caption\nsecond caption" if shared_caption is None
        else shared_caption or "📎 second.png"
    )
    assert adapter._upload_file.await_count == (2 if existing_file else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("human_delay", [0.125, 0.0, -0.25])
async def test_batch_fallback_waits_before_each_single_file_send(
    monkeypatch, tmp_path, human_delay
):
    import asyncio

    adapter = _make_adapter()
    images = []
    for name in ("first.png", "second.png"):
        path = tmp_path / name
        path.write_bytes(b"png")
        images.append((path.as_uri(), name))
    events = []
    payloads = []
    upload_ids = iter(("file-1", "file-2", "file-3", "file-4"))

    async def upload(_chat_id, _data, name, _content_type):
        events.append(("upload", name))
        return next(upload_ids)

    async def post(_path, payload):
        payloads.append(dict(payload))
        events.append(("post", tuple(payload["file_ids"])))
        if len(payloads) == 1:
            adapter._last_post_status = 400  # Confirmed batch rejection, no post ID.
            return {}
        adapter._last_post_status = 201
        return {"id": f"post-{len(payloads)}"}

    sleep = AsyncMock(side_effect=lambda delay: events.append(("sleep", delay)))
    monkeypatch.setattr(asyncio, "sleep", sleep)
    adapter._upload_file = AsyncMock(side_effect=upload)
    adapter._api_post = AsyncMock(side_effect=post)

    result = await adapter.send_multiple_images(
        "channel-1", images, human_delay=human_delay,
    )

    assert result.success is True
    assert result.raw_response["message_ids"] == ("post-2", "post-3")
    expected = [
        ("upload", "first.png"), ("upload", "second.png"),
        ("post", ("file-1", "file-2")),
    ]
    for name, file_id in (("first.png", "file-3"), ("second.png", "file-4")):
        if human_delay > 0:
            expected.append(("sleep", human_delay))
        expected.extend([("upload", name), ("post", (file_id,))])
    assert events == expected
    assert sleep.await_count == (2 if human_delay > 0 else 0)


@pytest.mark.asyncio
async def test_shared_caption_survives_unposted_single_fallbacks(monkeypatch, tmp_path):
    import sys
    from tools.send_message_tool import _send_to_platform
    adapter = _make_adapter({"max_post_length": 500})
    files = [tmp_path / f"image-{i}.png" for i in range(6)]
    for path in files:
        path.write_bytes(b"png")
    adapter._upload_file = AsyncMock(side_effect=[f"file-{i}" for i in range(6)])
    payloads = []
    async def post(endpoint, payload):
        payloads.append(dict(payload))
        if len(payloads) == 1:
            for path in files[:5]:
                path.unlink()
            adapter._last_post_status = 400
            return {}
        return {"id": "last-batch-post"}
    adapter._api_post = AsyncMock(side_effect=post)
    runner = SimpleNamespace(adapters={Platform.MATTERMOST: adapter})
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_gateway_runner_ref=lambda: runner))
    result = await _send_to_platform(
        Platform.MATTERMOST, adapter.config, "channel-1", "caption must survive",
        media_files=[(str(path), False) for path in files],
    )
    assert result["success"] is False
    assert result["media_delivered"] is False
    assert result["partial_failure"] is True
    assert result["message_ids"] == ["last-batch-post"]
    assert len(payloads) == 2
    assert payloads[-1]["file_ids"] == ["file-5"]
    assert payloads[-1]["message"] == "caption must survive"
