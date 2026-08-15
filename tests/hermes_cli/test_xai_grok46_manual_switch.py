"""Manual-only Grok 4.6 switching contract.

The explicit provider path must use xAI OAuth directly without consulting
automatic provider detection or configured-provider inference.
"""

from unittest.mock import patch

from hermes_cli.model_switch import switch_model


_ACCEPTED = {
    "accepted": True,
    "persist": True,
    "recognized": True,
    "message": None,
}


def test_explicit_xai_oauth_grok46_switch_bypasses_provider_detection():
    def unexpected_detection(*_args, **_kwargs):
        raise AssertionError("explicit xai-oauth switch must not infer a provider")

    with (
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "unit-credential",
                "base_url": "https://api.x.ai/v1",
                "api_mode": "codex_responses",
            },
        ),
        patch("hermes_cli.models.validate_requested_model", return_value=_ACCEPTED),
        patch("hermes_cli.models.detect_provider_for_model", unexpected_detection),
        patch("hermes_cli.model_switch.get_model_info", return_value=None),
        patch("hermes_cli.model_switch.get_model_capabilities", return_value=None),
    ):
        result = switch_model(
            raw_input="grok-4.6",
            current_provider="openai-codex",
            current_model="gpt-5.6-sol",
            current_base_url="https://api.openai.com/v1",
            current_api_key="current-credential",
            explicit_provider="xai-oauth",
            user_providers={},
            custom_providers=[],
        )

    assert result.success is True, result.error_message
    assert result.provider_changed is True
    assert result.target_provider == "xai-oauth"
    assert result.new_model == "grok-4.6"
    assert result.base_url == "https://api.x.ai/v1"
    assert result.api_mode == "codex_responses"
    assert result.api_key == "unit-credential"
