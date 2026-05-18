import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

moa = importlib.import_module("tools.mixture_of_agents_tool")


def _fake_response(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, reasoning=None))]
    )


def _fake_async_client(calls, provider_label):
    async def create(**kwargs):
        calls.append((provider_label, kwargs))
        return _fake_response(f"response from {provider_label}:{kwargs['model']}")

    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        ),
        base_url="https://example.invalid/v1",
    )


def test_moa_defaults_use_five_provider_aware_sota_experts_and_current_judge():
    assert moa.REFERENCE_MODELS == [
        {"provider": "openai-codex", "model": "gpt-5.5"},
        {"provider": "anthropic", "model": "claude-opus-4.6"},
        {"provider": "openrouter", "model": "google/gemini-3.1-pro-preview"},
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
        {"provider": "openrouter", "model": "moonshotai/kimi-k2.6"},
    ]
    assert moa.AGGREGATOR_MODEL == {"provider": "main", "model": "current"}


def test_current_judge_provider_only_config_uses_provider_specific_default():
    with patch("hermes_cli.config.load_config", return_value={"model": {"provider": "anthropic"}}):
        assert moa._read_current_main_model_spec() == {
            "provider": "anthropic",
            "model": "claude-opus-4.6",
        }


def test_current_judge_legacy_vendor_slug_without_provider_uses_openrouter():
    with patch("hermes_cli.config.load_config", return_value={"model": "anthropic/claude-opus-4.6"}):
        assert moa._read_current_main_model_spec() == {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.6",
        }


def test_current_judge_dict_model_key_is_honored():
    with patch(
        "hermes_cli.config.load_config",
        return_value={"model": {"provider": "anthropic", "model": "claude-sonnet-4.6"}},
    ):
        assert moa._read_current_main_model_spec() == {
            "provider": "anthropic",
            "model": "claude-sonnet-4.6",
        }


@pytest.mark.asyncio
async def test_reference_model_uses_provider_router_and_preserves_retry_logging(monkeypatch):
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(side_effect=RuntimeError("rate limited"))
            )
        ),
        base_url="https://openrouter.ai/api/v1",
    )
    resolve = MagicMock(return_value=(fake_client, "deepseek/deepseek-v4-pro"))
    warn = MagicMock()
    err = MagicMock()

    monkeypatch.setattr(moa, "resolve_provider_client", resolve)
    monkeypatch.setattr(moa.logger, "warning", warn)
    monkeypatch.setattr(moa.logger, "error", err)

    model, message, success = await moa._run_reference_model_safe(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
        "hello",
        max_retries=2,
    )

    assert model == "openrouter/deepseek/deepseek-v4-pro"
    assert success is False
    assert "failed after 2 attempts" in message
    resolve.assert_called_with("openrouter", "deepseek/deepseek-v4-pro", async_mode=True)
    assert warn.call_count == 2
    assert all(call.kwargs.get("exc_info") is None for call in warn.call_args_list)
    err.assert_called_once()
    assert err.call_args.kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_moa_queries_five_references_then_current_main_judge(monkeypatch):
    calls = []

    def fake_resolve(provider, model=None, async_mode=False):
        assert async_mode is True
        return _fake_async_client(calls, provider), model

    monkeypatch.setattr(moa, "resolve_provider_client", fake_resolve)
    monkeypatch.setattr(
        moa,
        "_read_current_main_model_spec",
        lambda: {"provider": "openai-codex", "model": "gpt-5.5"},
    )
    monkeypatch.setattr(
        moa,
        "_debug",
        SimpleNamespace(log_call=MagicMock(), save=MagicMock(), active=False),
    )

    result = json.loads(await moa.mixture_of_agents_tool("solve this"))

    assert result["success"] is True
    assert len(result["models_used"]["reference_models"]) == 5
    assert result["models_used"]["aggregator_model"] == {
        "provider": "openai-codex",
        "model": "gpt-5.5",
    }
    reference_calls = calls[:5]
    judge_call = calls[5]
    assert [provider for provider, _ in reference_calls] == [
        "openai-codex",
        "anthropic",
        "openrouter",
        "openrouter",
        "openrouter",
    ]
    assert judge_call[0] == "openai-codex"
    assert judge_call[1]["messages"][0]["role"] == "system"
    assert "Responses from models:" in judge_call[1]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_moa_top_level_error_logs_single_traceback_on_aggregator_failure(monkeypatch):
    monkeypatch.setattr(
        moa,
        "_run_reference_model_safe",
        AsyncMock(return_value=("anthropic/claude-opus-4.6", "ok", True)),
    )
    monkeypatch.setattr(
        moa,
        "_run_aggregator_model",
        AsyncMock(side_effect=RuntimeError("aggregator boom")),
    )
    monkeypatch.setattr(
        moa,
        "_debug",
        SimpleNamespace(log_call=MagicMock(), save=MagicMock(), active=False),
    )

    err = MagicMock()
    monkeypatch.setattr(moa.logger, "error", err)

    result = json.loads(
        await moa.mixture_of_agents_tool(
            "solve this",
            reference_models=[{"provider": "anthropic", "model": "claude-opus-4.6"}],
        )
    )

    assert result["success"] is False
    assert "Error in MoA processing" in result["error"]
    err.assert_called_once()
    assert err.call_args.kwargs.get("exc_info") is True
