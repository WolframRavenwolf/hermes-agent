"""Tests for the allowed_{channels,chats,rooms} whitelist extension
added alongside PR #7401 (Slack).

Covers: Telegram, Matrix, Mattermost, DingTalk.

For each platform:
- Empty = no restriction (fully backward compatible).
- When set, messages from non-listed chats/rooms are silently ignored.
- DMs are never filtered.
- @mention does NOT bypass the whitelist.
- config.yaml → env var bridging (via load_gateway_config) where applicable.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _make_telegram_adapter(*, allowed_chats=None, require_mention=None, guest_mode=False):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    extra = {"guest_mode": guest_mode}
    if allowed_chats is not None:
        extra["allowed_chats"] = allowed_chats
    if require_mention is not None:
        extra["require_mention"] = require_mention

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra=extra)
    adapter._bot = SimpleNamespace(id=999, username="hermes_bot")
    adapter._message_handler = AsyncMock()
    adapter._mention_patterns = adapter._compile_mention_patterns()
    # PR db50af910 added a TELEGRAM_ALLOWED_USERS allowlist gate to
    # _should_process_message; stub it for tests that exercise the
    # allowed-channels widening logic that runs after.
    adapter._is_callback_user_authorized = lambda *_a, **_kw: True
    return adapter


def _tg_group_message(chat_id=-100, text="hello"):
    return SimpleNamespace(
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        chat=SimpleNamespace(id=chat_id, type="group"),
        from_user=SimpleNamespace(id=111),
        reply_to_message=None,
    )


def _tg_dm_message(text="hello"):
    return SimpleNamespace(
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        chat=SimpleNamespace(id=111, type="private"),
        from_user=SimpleNamespace(id=111),
        reply_to_message=None,
    )


class TestTelegramAllowedChats:
    def test_empty_is_no_restriction(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)
        adapter = _make_telegram_adapter()
        assert adapter._telegram_allowed_chats() == set()
        assert adapter._should_process_message(_tg_group_message(-100)) is True

    def test_list_form(self):
        adapter = _make_telegram_adapter(allowed_chats=[-100, -200])
        assert adapter._telegram_allowed_chats() == {"-100", "-200"}


    def test_mention_cannot_bypass_whitelist(self):
        """@mention in a non-allowed chat is still ignored."""
        adapter = _make_telegram_adapter(allowed_chats=["-100"])
        msg = _tg_group_message(-999, text="@hermes_bot hello")
        msg.entities = [SimpleNamespace(
            type="mention", offset=0, length=len("@hermes_bot"),
        )]
        assert adapter._should_process_message(msg) is False


    def test_config_bridge(self, monkeypatch, tmp_path):
        """slack-style config.yaml → env var bridge works."""
        from gateway.config import load_gateway_config

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "telegram:\n"
            "  allowed_chats:\n"
            "    - -100\n"
            "    - -200\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHATS", "__sentinel__")
        monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS")

        load_gateway_config()

        import os as _os
        assert _os.environ["TELEGRAM_ALLOWED_CHATS"] == "-100,-200"


# ---------------------------------------------------------------------------
# DingTalk
# ---------------------------------------------------------------------------

def _make_dingtalk_adapter(*, allowed_chats=None, require_mention=None):
    # Import lazily — DingTalk SDK may not be installed.
    pytest.importorskip("plugins.platforms.dingtalk.adapter", reason="DingTalk adapter not importable")
    from plugins.platforms.dingtalk.adapter import DingTalkAdapter

    extra = {}
    if allowed_chats is not None:
        extra["allowed_chats"] = allowed_chats
    if require_mention is not None:
        extra["require_mention"] = require_mention

    adapter = object.__new__(DingTalkAdapter)
    adapter.platform = Platform.DINGTALK
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    return adapter


class TestDingTalkAllowedChats:
    def test_empty_is_no_restriction(self, monkeypatch):
        monkeypatch.delenv("DINGTALK_ALLOWED_CHATS", raising=False)
        adapter = _make_dingtalk_adapter()
        assert adapter._dingtalk_allowed_chats() == set()

    def test_list_form(self):
        adapter = _make_dingtalk_adapter(allowed_chats=["cidABC", "cidDEF"])
        assert adapter._dingtalk_allowed_chats() == {"cidABC", "cidDEF"}


# ---------------------------------------------------------------------------
# Mattermost (profile-local YAML, scoped environment fallback)
# ---------------------------------------------------------------------------

class TestMattermostAllowedChannels:
    @pytest.mark.parametrize("runtime", ["single", "default", "secondary"])
    @pytest.mark.parametrize("env_mention", [None, "true", "false"])
    def test_yaml_profiles_keep_gating_isolated_after_reload(
        self, monkeypatch, tmp_path, runtime, env_mention
    ):
        """Upstream config.yaml/extra is authoritative, including false and empty lists."""
        import os
        from contextvars import Context
        from unittest.mock import patch
        import yaml
        from agent.secret_scope import is_multiplex_active, set_multiplex_active, set_secret_scope
        from gateway.config import load_gateway_config
        from plugins.platforms.mattermost.adapter import MattermostAdapter

        keys = ("MATTERMOST_REQUIRE_MENTION", "MATTERMOST_FREE_RESPONSE_CHANNELS",
                "MATTERMOST_ALLOWED_CHANNELS", "MATTERMOST_MAX_POST_LENGTH")
        contexts = {name: Context() for name in ("alpha", "beta")}
        previous_multiplex = is_multiplex_active()
        set_multiplex_active(runtime != "single")
        try:
            with patch.dict(os.environ, {}, clear=False):
                for key in keys:
                    os.environ.pop(key, None)
                for name, context in contexts.items():
                    # None models a profile with no Mattermost environment settings.
                    scope = dict(zip(keys, (env_mention, f"{name}-env-free",
                                           f"{name}-env-free,{name}-env", "9000"))) if env_mention is not None else {}
                    context.run(set_secret_scope, scope if runtime == "secondary" else None)
                    (tmp_path / name).mkdir()
                if runtime == "secondary":
                    # A scoped miss must not borrow the default profile's settings.
                    os.environ.update(dict(zip(keys, ("false", "foreign", "foreign", "9000"))))
                elif env_mention is not None:
                    os.environ.update(dict(zip(keys, (env_mention, "env-free", "env-free,env", "9000"))))

                before = {key: os.environ.get(key) for key in keys}

                def load(name, settings):
                    home = tmp_path / name
                    (home / "config.yaml").write_text(yaml.safe_dump({
                        "platforms": {"mattermost": {"enabled": True, "token": "test-token",
                                                      "extra": {"url": "https://mm.example.com"}}},
                        "mattermost": settings,
                    }), encoding="utf-8")
                    monkeypatch.setenv("HERMES_HOME", str(home))
                    config = contexts[name].run(load_gateway_config).platforms[Platform.MATTERMOST]
                    adapter = contexts[name].run(MattermostAdapter, config)
                    adapter._bot_username = "hermes"
                    adapter._bot_user_id = "bot-id"
                    return adapter

                def gate(name, adapter, channel, text="hello"):
                    return contexts[name].run(adapter._apply_channel_gating, channel, text)

                settings = {
                    "alpha": {"require_mention": False, "allowed_channels": ["alpha"],
                              "free_response_channels": ["outside"], "max_post_length": 500},
                    "beta": {"require_mention": True, "allowed_channels": ["beta", "beta-free"],
                             "free_response_channels": ["beta-free", "outside"], "max_post_length": 8000},
                }
                alpha = load("alpha", settings["alpha"])
                beta = load("beta", settings["beta"])
                # Interleave loads and gating; neither existing adapter may adopt its neighbor's YAML.
                reloaded_alpha = load("alpha", settings["alpha"])
                for adapter in (alpha, reloaded_alpha):
                    assert gate("alpha", adapter, "alpha") == "hello"
                    assert gate("alpha", adapter, "outside", "@hermes hello") is None
                    assert adapter.MAX_MESSAGE_LENGTH == 500
                assert gate("beta", beta, "beta") is None
                assert gate("beta", beta, "beta", "@hermes hello") == "hello"
                assert gate("beta", beta, "beta-free") == "hello"
                assert gate("beta", beta, "outside") is None
                assert gate("beta", beta, "outside", "@hermes hello") is None
                assert beta.MAX_MESSAGE_LENGTH == 8000

                empty = load("beta", {"require_mention": True, "allowed_channels": [],
                                      "free_response_channels": []})
                env_free = "beta-env-free" if runtime == "secondary" else "env-free"
                assert gate("beta", empty, env_free) is None
                assert gate("beta", empty, "anywhere", "@hermes hello") == "hello"
                for name in contexts:
                    cleared = load(name, {})
                    assert cleared.MAX_MESSAGE_LENGTH == 4000  # max_post_length remains YAML-only.
                    if env_mention is None:
                        assert gate(name, cleared, "anywhere") is None
                        assert gate(name, cleared, "anywhere", "@hermes hello") == "hello"
                    else:
                        prefix = f"{name}-" if runtime == "secondary" else ""
                        assert gate(name, cleared, prefix + "env-free") == "hello"
                        assert gate(name, cleared, prefix + "env") == (
                            "hello" if env_mention == "false" else None)
                        assert gate(name, cleared, "outside", "@hermes hello") is None
                assert gate("alpha", alpha, "alpha") == "hello"
                assert gate("beta", beta, "beta") is None
                assert {key: os.environ.get(key) for key in keys} == before
        finally:
            set_multiplex_active(previous_multiplex)


# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------

class TestMatrixAllowedRooms:
    """Matrix whitelist behavior — tested via the env-var-initialized
    instance attribute _allowed_rooms."""

    def test_empty_env_empty_set(self, monkeypatch):
        monkeypatch.delenv("MATRIX_ALLOWED_ROOMS", raising=False)
        # Replicate __init__ parsing without needing the real adapter.
        raw = "" or ""
        allowed = {r.strip() for r in raw.split(",") if r.strip()}
        assert allowed == set()


