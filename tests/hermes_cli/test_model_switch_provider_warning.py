"""Implicit /model provider changes must be avoided or made conspicuous."""

from typing import Any
from unittest.mock import Mock

import pytest

from hermes_cli import models
from hermes_cli.model_switch import switch_model


@pytest.fixture
def offline_switch(monkeypatch):
    # Only external I/O is replaced; exercise the real detection and switch paths.
    monkeypatch.setattr("hermes_cli.model_switch.resolve_alias", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.model_switch.list_provider_models", lambda *a, **kw: [])
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.model_switch.get_model_capabilities", lambda *a, **kw: None)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: {"provider": kw.get("requested"), "api_key": "test", "base_url": "https://example.invalid/v1", "api_mode": "chat_completions"},
    )
    monkeypatch.setattr(models, "validate_requested_model", lambda *a, **kw: {
        "accepted": True, "persist": True, "recognized": True, "message": "Existing validation warning",
    })
    monkeypatch.setattr(models, "_PROVIDER_MODELS", {
        "openai-codex": ["old-model"], "openrouter": ["openai/gpt-6-astra"],
    })
    monkeypatch.setattr(models, "_find_openrouter_slug", lambda name: "openai/gpt-6-astra")
    catalog = Mock(return_value=[])
    monkeypatch.setattr(models, "provider_model_ids", catalog)
    return catalog


def _switch(**overrides):
    args: dict[str, Any] = dict(raw_input="gpt-6-astra", current_provider="openai-codex", current_model="old-model")
    args.update(overrides)
    result = switch_model(**args)
    assert result.success, result.error_message
    return result


def test_live_codex_model_stays_on_codex(offline_switch):
    offline_switch.return_value = ["gpt-6-astra"]
    result = _switch()
    assert result.target_provider == "openai-codex"
    assert result.new_model == "gpt-6-astra"
    assert not result.provider_switch_warning
    offline_switch.assert_called_once_with("openai-codex")


@pytest.mark.parametrize("catalog_error", [False, True])
def test_openrouter_fallback_warns_when_live_catalog_cannot_match(offline_switch, catalog_error):
    if catalog_error:
        offline_switch.side_effect = RuntimeError("catalog unavailable")
    result = _switch()
    assert result.target_provider == "openrouter"
    assert result.new_model == "openai/gpt-6-astra"
    assert "openai-codex -> openrouter" in result.provider_switch_warning
    assert "--provider" in result.provider_switch_warning
    assert "additional costs" in result.provider_switch_warning
    assert result.warning_message == "Existing validation warning"


def test_explicit_provider_change_does_not_warn(offline_switch):
    result = _switch(explicit_provider="openrouter")
    assert result.target_provider == "openrouter"
    assert not result.provider_switch_warning
    offline_switch.assert_not_called()


def test_same_provider_does_not_warn(offline_switch):
    result = _switch(raw_input="old-model")
    assert result.target_provider == "openai-codex"
    assert not result.provider_switch_warning
    offline_switch.assert_not_called()


def test_config_routing_cannot_disguise_implicit_provider_change(offline_switch):
    # The pipeline internally sets explicit_provider to resolve this provider's
    # credentials, even though the user supplied no --provider argument.
    result = _switch(user_providers={"work-api": {
        "base_url": "https://example.invalid/v1", "api_key": "test", "models": ["gpt-6-astra"],
    }})
    assert result.target_provider == "work-api"
    assert "openai-codex -> work-api" in result.provider_switch_warning


def test_canonical_provider_alias_does_not_warn(offline_switch, monkeypatch):
    monkeypatch.setattr(models, "detect_provider_for_model", lambda *a: ("openrouter", "openai/gpt-6-astra"))
    result = _switch(current_provider="openai")
    assert result.target_provider == "openrouter"
    assert not result.provider_switch_warning


def test_live_ollama_cloud_suffix_keeps_current_provider(offline_switch):
    offline_switch.return_value = ["new-cloud-model:cloud"]
    assert models.detect_provider_for_model("new-cloud-model", "ollama-cloud") is None
