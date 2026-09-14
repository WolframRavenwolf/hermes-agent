"""Regression tests for issue #21419.

`_resolve_explicit_runtime()` must re-derive `api_mode` from the effective
model (target_model > model_cfg.default) for opencode-zen / opencode-go,
the same way `_resolve_runtime_from_pool_entry()` already does. Without
this, a stale `api_mode: anthropic_messages` from a previous Claude
session leaks onto a non-Claude opencode model, sending Anthropic-format
requests to a chat_completions endpoint and getting 404.
"""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from hermes_cli import runtime_provider as rp
from hermes_cli.runtime_provider import _resolve_explicit_runtime


def _resolve(provider: str, model_default: str, configured_api_mode: str = "anthropic_messages",
             *, target_model: str = None):
    return _resolve_explicit_runtime(
        provider=provider,
        requested_provider=provider,
        model_cfg={"default": model_default, "api_mode": configured_api_mode},
        explicit_api_key="sk-test",
        explicit_base_url="https://opencode.ai/zen/v1" if provider == "opencode-zen" else "https://opencode.ai/zen/go/v1",
        target_model=target_model,
    )


def test_opencode_zen_non_claude_overrides_stale_anthropic_api_mode():
    """The bug: config has api_mode=anthropic_messages from previous Claude session,
    but the active model is non-Claude — must resolve to chat_completions."""
    runtime = _resolve("opencode-zen", "big-pickle", configured_api_mode="anthropic_messages")
    assert runtime is not None
    assert runtime["api_mode"] == "chat_completions"


def test_opencode_zen_claude_model_resolves_anthropic_messages():
    runtime = _resolve("opencode-zen", "claude-sonnet-4-5", configured_api_mode="chat_completions")
    assert runtime is not None
    assert runtime["api_mode"] == "anthropic_messages"
    # The /v1 strip must apply: Anthropic SDK appends /v1/messages, so the
    # base_url should end at /zen (no trailing /v1) to avoid /v1/v1/messages.
    assert not runtime["base_url"].endswith("/v1")
    assert runtime["base_url"].endswith("/zen")


def test_opencode_zen_gpt_model_resolves_codex_responses():
    runtime = _resolve("opencode-zen", "gpt-5", configured_api_mode="anthropic_messages")
    assert runtime is not None
    assert runtime["api_mode"] == "codex_responses"


def test_opencode_go_minimax_resolves_anthropic_messages():
    runtime = _resolve("opencode-go", "minimax-m2", configured_api_mode="chat_completions")
    assert runtime is not None
    assert runtime["api_mode"] == "anthropic_messages"


def test_opencode_go_non_minimax_resolves_chat_completions():
    runtime = _resolve("opencode-go", "glm-4.6", configured_api_mode="anthropic_messages")
    assert runtime is not None
    assert runtime["api_mode"] == "chat_completions"


def test_target_model_overrides_model_cfg_default():
    """A mid-session /model switch passes target_model — that must win
    over the persisted config default."""
    runtime = _resolve(
        "opencode-zen",
        "claude-sonnet-4-5",  # stale persisted default
        configured_api_mode="anthropic_messages",
        target_model="big-pickle",  # the model we are switching TO
    )
    assert runtime is not None
    assert runtime["api_mode"] == "chat_completions"


def test_non_opencode_provider_unchanged():
    """Sanity: providers that are not opencode-zen / opencode-go must still
    honour the configured api_mode (no behavioural change)."""
    runtime = _resolve_explicit_runtime(
        provider="kilocode",
        requested_provider="kilocode",
        model_cfg={"default": "anything", "api_mode": "anthropic_messages"},
        explicit_api_key="sk-test",
        explicit_base_url="https://api.kilo.ai/api/gateway",
    )
    assert runtime is not None
    assert runtime["api_mode"] == "anthropic_messages"
    assert runtime["base_url"] == "https://api.kilo.ai/api/gateway"


@pytest.mark.parametrize(
    "provider,target_model,model_default,configured_api_mode,expected_mode",
    [
        ("opencode-zen", "gpt-5", "claude-sonnet-4-5", "anthropic_messages", "codex_responses"),
        ("opencode-zen", "big-pickle", "claude-sonnet-4-5", "anthropic_messages", "chat_completions"),
        ("opencode-go", "glm-4.6", "minimax-m2", "anthropic_messages", "chat_completions"),
        ("opencode-zen", "claude-sonnet-4-5", "gpt-5", "codex_responses", "anthropic_messages"),
        ("opencode-go", "minimax-m2", "glm-4.6", "chat_completions", "anthropic_messages"),
    ],
)
@pytest.mark.parametrize("endpoint,restored_suffix", [
    ("https://opencode.ai/zen", "/v1"),
    ("https://opencode.ai/zen/go", "/v1"),
    ("https://proxy.example.com", ""),
    ("https://proxy.example.com/v1/proxy/zen", ""),
    ("https://proxy.example/openai", ""),
])
@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/", "/v1//"])
def test_opencode_explicit_endpoint_matches_target_transport(
    monkeypatch, provider, target_model, model_default, configured_api_mode,
    expected_mode, endpoint, restored_suffix, suffix,
):
    config = {"model": {
        "provider": provider,
        "default": model_default,
        "api_mode": configured_api_mode,
        "base_url": "https://config.example.com/unused/v1",
        "extra_headers": {"X-Test-Route": "explicit"},
        "reasoning_effort": "high",
        "verbosity": "low",
    }}
    original_config = deepcopy(config)
    monkeypatch.setattr(rp, "load_config", lambda: config)
    pool = Mock(return_value=None)
    monkeypatch.setattr(rp, "load_pool", pool)
    monkeypatch.setenv(
        rp.PROVIDER_REGISTRY[provider].base_url_env_var,
        "https://env.example.com/unused/v1",
    )

    runtime = rp.resolve_runtime_provider(
        requested=provider,
        explicit_api_key="sk-test",
        explicit_base_url=endpoint + suffix,
        target_model=target_model,
    )

    # Custom roots remain exact after trailing cleanup; only the two known
    # OpenCode roots regain /v1. The previous broad proxy oracle was wrong.
    expected_url = endpoint
    if expected_mode != "anthropic_messages":
        expected_url += suffix.rstrip("/") or restored_suffix
    assert runtime == {
        "provider": provider,
        "requested_provider": provider,
        "api_mode": expected_mode,
        "base_url": expected_url,
        "api_key": "sk-test",
        "source": "explicit",
    }
    assert rp.resolve_runtime_provider(
        requested=provider,
        explicit_api_key=runtime["api_key"],
        explicit_base_url=runtime["base_url"],
        target_model=target_model,
    ) == runtime

    if expected_mode == "anthropic_messages":
        switched = rp.resolve_runtime_provider(
            requested=provider,
            explicit_api_key=runtime["api_key"],
            explicit_base_url=runtime["base_url"],
            target_model=model_default,
        )
        assert switched == {
            **runtime,
            "api_mode": configured_api_mode,
            "base_url": endpoint + restored_suffix,
        }
        assert rp.resolve_runtime_provider(
            requested=provider,
            explicit_api_key=switched["api_key"],
            explicit_base_url=switched["base_url"],
            target_model=model_default,
        ) == switched
    assert config == original_config
    pool.assert_not_called()


@pytest.mark.parametrize(
    "provider,anthropic_model,next_model,next_mode,endpoint",
    [
        ("opencode-zen", "claude-sonnet-4-5", "gpt-5", "codex_responses", "https://opencode.ai/zen"),
        ("opencode-go", "minimax-m2", "glm-4.6", "chat_completions", "https://opencode.ai/zen/go"),
    ],
)
def test_opencode_switch_restores_v1_on_previous_resolved_endpoint(
    provider, anthropic_model, next_model, next_mode, endpoint,
):
    first = _resolve_explicit_runtime(
        provider=provider,
        requested_provider=provider,
        model_cfg={"default": next_model, "api_mode": next_mode},
        explicit_api_key="sk-test-switch",
        explicit_base_url=endpoint + "/v1/",
        target_model=anthropic_model,
    )
    assert first is not None
    assert first["api_mode"] == "anthropic_messages"
    assert first["base_url"] == endpoint

    switched = _resolve_explicit_runtime(
        provider=first["provider"],
        requested_provider=first["requested_provider"],
        model_cfg={"default": anthropic_model, "api_mode": first["api_mode"]},
        explicit_api_key=first["api_key"],
        explicit_base_url=first["base_url"],
        target_model=next_model,
    )
    assert switched == {
        **first,
        "api_mode": next_mode,
        "base_url": endpoint + "/v1",
    }


@pytest.mark.parametrize("api_mode", ["chat_completions", "codex_responses"])
@pytest.mark.parametrize("base_url", ["https://api.kilo.ai/api/gateway", "https://api.kilo.ai/api/gateway/v1"])
def test_non_opencode_non_anthropic_endpoint_unchanged(api_mode, base_url):
    runtime = _resolve_explicit_runtime(
        provider="kilocode",
        requested_provider="kilocode",
        model_cfg={"default": "anything", "api_mode": api_mode},
        explicit_api_key="sk-test",
        explicit_base_url=base_url,
        target_model="claude-sonnet-4-5",
    )
    assert runtime == {
        "provider": "kilocode",
        "requested_provider": "kilocode",
        "api_mode": api_mode,
        "base_url": base_url,
        "api_key": "sk-test",
        "source": "explicit",
    }


@pytest.mark.parametrize(
    "provider,target_model,model_default,expected_mode",
    [
        ("opencode-zen", "gpt-5", "claude-sonnet-4-5", "codex_responses"),
        ("opencode-go", "glm-4.6", "minimax-m2", "chat_completions"),
    ],
)
@pytest.mark.parametrize("endpoint", [
    "https://proxy.example.com",
    "https://proxy.example.com/v1/proxy/zen",
    "https://proxy.example/openai",
])
def test_explicit_custom_root_overrides_same_provider_config_v1(
    monkeypatch, provider, target_model, model_default, expected_mode, endpoint,
):
    config = {"model": {
        "provider": provider,
        "default": model_default,
        "api_mode": "anthropic_messages",
        "base_url": endpoint + "/v1",
        "extra_headers": {"X-Test-Route": "explicit"},
        "reasoning_effort": "high",
        "verbosity": "low",
    }}
    original_config = deepcopy(config)
    monkeypatch.setattr(rp, "load_config", lambda: config)
    pool = Mock(return_value=None)
    monkeypatch.setattr(rp, "load_pool", pool)
    monkeypatch.setenv(
        rp.PROVIDER_REGISTRY[provider].base_url_env_var,
        "https://env.example.com/unused/v1",
    )

    runtime = rp.resolve_runtime_provider(
        requested=provider,
        explicit_api_key="sk-test",
        explicit_base_url=endpoint,
        target_model=target_model,
    )

    assert runtime == {
        "provider": provider,
        "requested_provider": provider,
        "api_mode": expected_mode,
        "base_url": endpoint,
        "api_key": "sk-test",
        "source": "explicit",
    }
    assert config == original_config
    pool.assert_not_called()


@pytest.mark.parametrize(
    "provider,model_default,configured_mode,expected_mode,official_root",
    [
        ("opencode-zen", "gpt-5", "anthropic_messages", "codex_responses", "https://opencode.ai/zen"),
        ("opencode-zen", "big-pickle", "anthropic_messages", "chat_completions", "https://opencode.ai/zen"),
        ("opencode-go", "glm-4.6", "anthropic_messages", "chat_completions", "https://opencode.ai/zen/go"),
        ("opencode-zen", "claude-sonnet-4-5", "codex_responses", "anthropic_messages", "https://opencode.ai/zen"),
        ("opencode-go", "minimax-m2", "chat_completions", "anthropic_messages", "https://opencode.ai/zen/go"),
    ],
)
@pytest.mark.parametrize("env_url", [None, "https://proxy.example/openai//", "https://proxy.example.com/v1/proxy/zen/v1//"])
def test_explicit_key_only_uses_env_or_official_default_and_config_model(
    monkeypatch, provider, model_default, configured_mode, expected_mode,
    official_root, env_url,
):
    config = {"model": {
        "provider": provider,
        "default": model_default,
        "api_mode": configured_mode,
        "base_url": "https://config.example.com/unused/v1",
        "extra_headers": {"X-Test-Route": "explicit"},
        "reasoning_effort": "high",
        "verbosity": "low",
    }}
    original_config = deepcopy(config)
    monkeypatch.setattr(rp, "load_config", lambda: config)
    pool = Mock(return_value=None)
    monkeypatch.setattr(rp, "load_pool", pool)
    env_name = rp.PROVIDER_REGISTRY[provider].base_url_env_var
    if env_url is None:
        monkeypatch.delenv(env_name, raising=False)
    else:
        monkeypatch.setenv(env_name, env_url)

    runtime = rp.resolve_runtime_provider(
        requested=provider,
        explicit_api_key="sk-test",
    )

    expected_url = env_url.rstrip("/") if env_url else official_root + "/v1"
    if expected_mode == "anthropic_messages" and expected_url.endswith("/v1"):
        expected_url = expected_url[:-3]
    assert runtime == {
        "provider": provider,
        "requested_provider": provider,
        "api_mode": expected_mode,
        "base_url": expected_url,
        "api_key": "sk-test",
        "source": "explicit",
    }
    assert config == original_config
    pool.assert_not_called()


@pytest.mark.parametrize("api_mode", ["anthropic_messages", "chat_completions", "codex_responses"])
@pytest.mark.parametrize("base_url", ["https://opencode.ai/zen", "https://api.kilo.ai/api/gateway/v1//"])
def test_public_kilocode_endpoint_keeps_configured_mode(monkeypatch, api_mode, base_url):
    config = {"model": {
        "provider": "kilocode",
        "default": "anything",
        "api_mode": api_mode,
        "base_url": "https://config.example.com/unused/v1",
    }}
    original_config = deepcopy(config)
    monkeypatch.setattr(rp, "load_config", lambda: config)
    pool = Mock(return_value=None)
    monkeypatch.setattr(rp, "load_pool", pool)

    runtime = rp.resolve_runtime_provider(
        requested="kilocode",
        explicit_api_key="sk-test",
        explicit_base_url=base_url,
        target_model="claude-sonnet-4-5",
    )

    assert runtime == {
        "provider": "kilocode",
        "requested_provider": "kilocode",
        "api_mode": api_mode,
        "base_url": base_url.rstrip("/"),
        "api_key": "sk-test",
        "source": "explicit",
    }
    assert config == original_config
    pool.assert_not_called()
