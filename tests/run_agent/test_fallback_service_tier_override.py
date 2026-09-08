"""Per-fallback tier isolation at native request-building boundaries."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class _CaptureTransport:
    def build_kwargs(self, **kwargs):
        return kwargs


def _request_boundary_agent(*, api_mode, provider="custom"):
    return SimpleNamespace(
        api_mode=api_mode,
        provider=provider,
        model="gpt-5.6",
        session_id="fallback-tier-test",
        tools=[],
        max_tokens=1024,
        reasoning_config=None,
        request_overrides={
            "service_tier": "priority",
            "speed": "fast",
            "extra_body": {"service_tier": "flex", "speed": "fast", "store": False},
            "custom": {"trace": True},
        },
        _active_fallback_service_tier_override="normal",
        base_url="https://example.test/v1",
        _base_url_lower="https://example.test/v1",
        _base_url_hostname="example.test",
        _ephemeral_max_output_tokens=None,
        _get_transport=lambda: _CaptureTransport(),
        _prepare_messages_for_non_vision_model=lambda messages: messages,
        _resolved_api_call_timeout=lambda: None,
        _github_models_reasoning_extra_body=lambda: None,
        _max_tokens_param=lambda _model: "max_tokens",
        _ollama_num_ctx=None,
        openrouter_min_coding_score=None,
        _supports_reasoning_extra_body=lambda: False,
        _is_qwen_portal=lambda: False,
        _is_openrouter_url=lambda: False,
        _qwen_prepare_chat_messages=lambda messages: messages,
        _qwen_prepare_chat_messages_inplace=lambda messages: messages,
        _codex_reasoning_replay_enabled=True,
    )


@pytest.mark.parametrize("api_mode,provider,has_profile", [
    ("chat_completions", "custom", False),
    ("chat_completions", "custom", True),
    ("codex_responses", "openai-codex", False),
])
@pytest.mark.parametrize("active_override", ["normal", None])
def test_transport_receives_route_local_copy(api_mode, provider, has_profile, active_override):
    from agent.chat_completion_helpers import build_api_kwargs

    agent = _request_boundary_agent(api_mode=api_mode, provider=provider)
    agent._active_fallback_service_tier_override = active_override
    before = deepcopy(agent.request_overrides)
    with (
        patch("providers.get_provider_profile", return_value=object() if has_profile else None),
        patch("agent.chat_completion_helpers._provider_preferences_for_agent", return_value=None),
    ):
        kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])

    effective = kwargs["request_overrides"]
    assert effective == ({"extra_body": {"store": False}, "custom": {"trace": True}}
                         if active_override == "normal" else before)
    # Even an unmodified primary policy must not alias transport-owned data.
    effective["custom"]["trace"] = False
    effective["extra_body"]["store"] = True
    assert agent.request_overrides == before


@pytest.mark.parametrize("active_override", ["normal", None])
def test_anthropic_request_boundary_uses_effective_fast_mode(active_override):
    from agent.chat_completion_helpers import build_api_kwargs
    from agent.transports.anthropic import AnthropicTransport
    from agent.anthropic_adapter import _FAST_MODE_BETA

    agent = SimpleNamespace(
        api_mode="anthropic_messages",
        provider="anthropic",
        model="claude-opus-4-6",
        session_id="fallback-tier-test",
        tools=None,
        max_tokens=1024,
        reasoning_config=None,
        request_overrides={"speed": "fast"},
        _active_fallback_service_tier_override=active_override,
        context_compressor=None,
        _ephemeral_max_output_tokens=None,
        _is_anthropic_oauth=False,
        _anthropic_base_url="https://api.anthropic.com",
        _oauth_1m_beta_disabled=False,
        _get_transport=lambda: AnthropicTransport(),
        _prepare_anthropic_messages_for_api=lambda messages: messages,
        _anthropic_preserve_dots=lambda: False,
    )
    kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
    assert (kwargs.get("extra_body") or {}).get("speed") == (
        None if active_override == "normal" else "fast"
    )
    betas = (kwargs.get("extra_headers") or {}).get("anthropic-beta", "")
    assert (_FAST_MODE_BETA in betas) is (active_override is None)
    assert agent.request_overrides == {"speed": "fast"}


@pytest.mark.parametrize("value", ["normal", " Normal ", "NORMAL", None])
def test_normalize_accepts_normal_or_disabled(value):
    from agent.fallback_policy import normalize_fallback_service_tier_override

    assert normalize_fallback_service_tier_override(value) == (None if value is None else "normal")


@pytest.mark.parametrize("value", ["priority", "fast", "", 7, {"tier": "normal"}])
def test_invalid_entry_warns_and_clears_previous_policy(value):
    from agent.fallback_policy import (
        activate_fallback_service_tier_override,
        normalize_fallback_service_tier_override,
    )

    with pytest.raises(ValueError, match="service_tier_override"):
        normalize_fallback_service_tier_override(value)
    agent = SimpleNamespace(_active_fallback_service_tier_override="normal")
    log = MagicMock()
    assert activate_fallback_service_tier_override(
        agent, {"service_tier_override": value}, log=log,
    ) is None
    assert agent._active_fallback_service_tier_override is None
    log.warning.assert_called_once()


@pytest.mark.parametrize("active_override", ["normal", None])
def test_policy_preserves_unrelated_nested_data_and_caller_ownership(active_override):
    from agent.fallback_policy import apply_fallback_service_tier_override

    original = {
        "service_tier": "priority", "speed": "fast", "custom_header": "keep-me",
        "extra_body": {
            "service_tier": "flex", "speed": "fast",
            "provider": {"order": ["one", "two"]},
        },
    }
    before = deepcopy(original)
    effective = apply_fallback_service_tier_override(original, active_override)
    assert effective == ({
        "custom_header": "keep-me", "extra_body": {"provider": {"order": ["one", "two"]}},
    } if active_override == "normal" else before)
    effective["extra_body"]["provider"]["order"].append("three")
    assert original == before
