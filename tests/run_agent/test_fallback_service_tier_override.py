"""Tests for per-fallback service-tier policy overrides."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.fallback_policy import (
    activate_fallback_service_tier_override,
    apply_fallback_service_tier_override,
    normalize_fallback_service_tier_override,
)


class _CaptureTransport:
    def build_kwargs(self, **kwargs):
        return kwargs


def _request_boundary_agent(*, api_mode: str, provider: str = "custom"):
    base_url = "https://example.test/v1"
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
            "extra_body": {"speed": "fast", "store": False},
            "custom": {"trace": True},
        },
        _active_fallback_service_tier_override="normal",
        base_url=base_url,
        _base_url_lower=base_url.lower(),
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


def test_normal_override_removes_only_tier_fields_without_mutating_input():
    original = {
        "service_tier": "priority",
        "speed": "fast",
        "custom_header": "keep-me",
        "extra_body": {
            "service_tier": "priority",
            "speed": "fast",
            "provider": {"order": ["one", "two"]},
        },
    }
    before = deepcopy(original)

    effective = apply_fallback_service_tier_override(original, "normal")

    assert effective == {
        "custom_header": "keep-me",
        "extra_body": {"provider": {"order": ["one", "two"]}},
    }
    assert original == before
    assert effective is not original
    assert effective["extra_body"] is not original["extra_body"]


def test_missing_active_override_preserves_values_in_a_deep_copy():
    original = {
        "service_tier": "priority",
        "extra_body": {"speed": "fast", "nested": {"value": 1}},
    }

    effective = apply_fallback_service_tier_override(original, None)

    assert effective == original
    assert effective is not original
    assert effective["extra_body"] is not original["extra_body"]
    assert effective["extra_body"]["nested"] is not original["extra_body"]["nested"]


@pytest.mark.parametrize("value", ["normal", " Normal ", "NORMAL"])
def test_normalize_accepts_only_case_insensitive_normal(value):
    assert normalize_fallback_service_tier_override(value) == "normal"


def test_normalize_missing_value_is_disabled():
    assert normalize_fallback_service_tier_override(None) is None


@pytest.mark.parametrize("value", ["priority", "fast", "", 7, {"tier": "normal"}])
def test_normalize_rejects_unknown_or_malformed_values(value):
    with pytest.raises(ValueError, match="service_tier_override"):
        normalize_fallback_service_tier_override(value)


def test_unknown_entry_warns_and_clears_a_previous_chain_policy():
    agent = SimpleNamespace(_active_fallback_service_tier_override="normal")
    log = MagicMock()

    result = activate_fallback_service_tier_override(
        agent,
        {"service_tier_override": "priority"},
        log=log,
    )

    assert result is None
    assert agent._active_fallback_service_tier_override is None
    log.warning.assert_called_once()


@pytest.mark.parametrize("has_profile", [False, True], ids=["legacy", "profile"])
def test_chat_completion_request_boundaries_receive_policy_clean_overrides(has_profile):
    from agent.chat_completion_helpers import build_api_kwargs

    agent = _request_boundary_agent(api_mode="chat_completions")
    profile = object() if has_profile else None
    with (
        patch("providers.get_provider_profile", return_value=profile),
        patch(
            "agent.chat_completion_helpers._provider_preferences_for_agent",
            return_value=None,
        ),
    ):
        kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])

    assert kwargs["request_overrides"] == {
        "extra_body": {"store": False},
        "custom": {"trace": True},
    }
    assert agent.request_overrides["service_tier"] == "priority"
    assert agent.request_overrides["extra_body"]["speed"] == "fast"


def test_codex_response_request_boundary_receives_policy_clean_overrides():
    from agent.chat_completion_helpers import build_api_kwargs

    agent = _request_boundary_agent(api_mode="codex_responses", provider="openai-codex")

    kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])

    assert kwargs["request_overrides"] == {
        "extra_body": {"store": False},
        "custom": {"trace": True},
    }
    assert agent.request_overrides["service_tier"] == "priority"


def test_anthropic_request_boundary_suppresses_fast_mode_for_normal_fallback():
    from agent.chat_completion_helpers import build_api_kwargs
    from agent.transports.anthropic import AnthropicTransport

    agent = SimpleNamespace(
        api_mode="anthropic_messages",
        provider="anthropic",
        model="claude-opus-4-6",
        session_id="fallback-tier-test",
        tools=None,
        max_tokens=1024,
        reasoning_config=None,
        request_overrides={"speed": "fast"},
        _active_fallback_service_tier_override="normal",
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

    assert "speed" not in kwargs
    assert "anthropic-beta" not in kwargs
    assert agent.request_overrides == {"speed": "fast"}
