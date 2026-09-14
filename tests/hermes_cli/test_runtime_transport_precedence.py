"""Runtime transport precedence: declared provider transport is the fallback.

The Coatue data-residency report (2026-07): pointing ``openai-api`` at
``us.api.openai.com`` silently fell back to ``chat_completions`` — every
tool-calling turn 400'd — because the runtime resolvers defaulted to
``chat_completions`` and consulted URL detection only, never the transport
the provider overlay itself declares.

Contract pinned here: when URL detection has no opinion, the runtime falls
back to ``providers.determine_api_mode(provider, base_url, model)`` (the
provider's declared transport), and only lands on ``chat_completions`` for
genuinely unknown providers/endpoints. Covers the explicit-runtime path and
the API-key-provider path; the pool-entry path shares the same helper.
"""

from __future__ import annotations

from unittest.mock import patch as mock_patch

import pytest

from hermes_cli.runtime_provider import _fallback_api_mode


class TestFallbackApiMode:
    @pytest.mark.parametrize(
        "base_url",
        [
            "https://api.openai.com/v1",
            "https://us.api.openai.com/v1",
            "https://eu.api.openai.com/v1",
        ],
    )
    def test_openai_api_official_hosts_resolve_codex_responses(self, base_url):
        assert _fallback_api_mode("openai-api", base_url) == "codex_responses"

    def test_openai_api_unknown_custom_proxy_still_uses_declared_transport(self):
        # Explicitly selected openai-api against a custom proxy keeps the
        # provider's declared transport (mirrors determine_api_mode semantics;
        # host identity is a separate question from provider selection).
        assert (
            _fallback_api_mode("openai-api", "https://proxy.corp.test/v1")
            == "codex_responses"
        )

    def test_lookalike_host_is_not_treated_as_official(self):
        # The spoof host must not be detected AS OpenAI by the URL lane —
        # the provider-declared transport may still apply, but host-derived
        # detection must return None for it.
        from hermes_cli.runtime_provider import _detect_api_mode_for_url

        assert _detect_api_mode_for_url("https://api.openai.com.attacker.test/v1") is None

    def test_openrouter_stays_chat_completions(self):
        assert _fallback_api_mode("openrouter", "https://openrouter.ai/api/v1") == "chat_completions"

    def test_minimax_declared_anthropic_transport_honored(self):
        # Same latent bug class: minimax declares an Anthropic-compatible
        # transport but previously fell back to chat_completions when the
        # URL carried no /anthropic hint.
        from hermes_cli.providers import determine_api_mode

        expected = determine_api_mode("minimax", "https://api.minimax.io")
        assert _fallback_api_mode("minimax", "https://api.minimax.io") == expected
        assert expected != "chat_completions" or expected == determine_api_mode("minimax", "")

    def test_unknown_provider_defaults_chat_completions(self):
        assert _fallback_api_mode("some-unknown", "https://example.test/v1") == "chat_completions"

    def test_url_detection_wins_over_provider_declaration(self):
        # /anthropic suffix on any provider routes anthropic_messages —
        # URL detection stays the higher-priority signal.
        assert (
            _fallback_api_mode("openai-api", "https://gateway.test/anthropic")
            == "anthropic_messages"
        )


class TestExplicitRuntimeIntegration:
    """The explicit-runtime path resolves regional OpenAI to codex_responses."""

    def test_explicit_openai_api_regional_host(self):
        from hermes_cli.runtime_provider import _resolve_explicit_runtime

        with mock_patch(
            "hermes_cli.runtime_provider._get_model_config",
            return_value={"provider": "openai-api", "default": "gpt-5.6-terra"},
        ):
            result = _resolve_explicit_runtime(
                provider="openai-api",
                requested_provider="openai-api",
                explicit_api_key="sk-test",
                explicit_base_url="https://us.api.openai.com/v1",
                model_cfg={"provider": "openai-api", "default": "gpt-5.6-terra"},
            )
        assert result is not None
        assert result["api_mode"] == "codex_responses"
        assert result["base_url"] == "https://us.api.openai.com/v1"


PROFILE_NAME = "transport-test"
PROFILE_ALIAS = "transport-test-alias"
PROFILE_URL = "https://gateway.example.test/api/coding"
PROFILE_KEY = "transport-test-key"
PROFILE_MODEL = "transport-test-model"
PROFILE_KEY_ENV = "TRANSPORT_TEST_API_KEY"
PROFILE_URL_ENV = "TRANSPORT_TEST_BASE_URL"


@pytest.fixture
def profile_catalog(monkeypatch):
    """Real profile lookup and catalog parsing, with an offline catalog cache."""
    import providers
    from agent import models_dev
    from providers.base import ProviderProfile

    providers.list_providers()
    monkeypatch.setattr(providers, "_REGISTRY", providers._REGISTRY.copy())
    monkeypatch.setattr(providers, "_ALIASES", providers._ALIASES.copy())
    monkeypatch.setattr(providers, "_PROVIDER_LIST_CACHE", None)
    profile = ProviderProfile(
        name=PROFILE_NAME,
        aliases=(PROFILE_ALIAS,),
        api_mode="anthropic_messages",
        env_vars=(PROFILE_KEY_ENV, PROFILE_URL_ENV),
        base_url=PROFILE_URL,
    )
    providers.register_provider(profile)
    # A nonempty fresh cache keeps fetch_models_dev on its real offline path.
    catalog = {"unrelated-test-provider": {"name": "Unrelated", "models": {}}}
    monkeypatch.setattr(models_dev, "_models_dev_cache", catalog)
    monkeypatch.setattr(models_dev, "_models_dev_cache_time", float("inf"))
    monkeypatch.delenv(PROFILE_URL_ENV, raising=False)
    monkeypatch.setenv(PROFILE_KEY_ENV, PROFILE_KEY)
    return profile, catalog


@pytest.fixture
def scoped_profile_env(monkeypatch):
    from agent import secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    values = {PROFILE_KEY_ENV: PROFILE_KEY}
    token = secret_scope.set_secret_scope(values)
    try:
        yield values
    finally:
        secret_scope.reset_secret_scope(token)


def _profile_pool_runtime(provider=PROFILE_NAME, base_url=PROFILE_URL, **config):
    from agent.credential_pool import PooledCredential
    from hermes_cli.runtime_provider import _resolve_runtime_from_pool_entry

    result = _resolve_runtime_from_pool_entry(
        provider=provider,
        requested_provider=provider,
        entry=PooledCredential(
            provider=provider,
            id="transport-test",
            label="Transport test",
            auth_type="api_key",
            priority=0,
            source="manual",
            access_token=PROFILE_KEY,
            base_url=base_url,
        ),
        model_cfg={"provider": provider, "default": PROFILE_MODEL, **config},
        target_model=PROFILE_MODEL,
    )
    assert result["provider"] == provider
    assert result["requested_provider"] == provider
    assert result["base_url"] == base_url.rstrip("/")
    assert result["api_key"] == PROFILE_KEY
    return result


@pytest.mark.parametrize("provider", [PROFILE_NAME, PROFILE_ALIAS])
@pytest.mark.parametrize(
    "declared,selected",
    [
        (PROFILE_URL, PROFILE_URL),
        (PROFILE_URL, PROFILE_URL + "/"),
        (" \t" + PROFILE_URL + "/ \n", PROFILE_URL),
        (PROFILE_URL, " \t" + PROFILE_URL + "/ \n"),
    ],
)
def test_literal_own_endpoint(profile_catalog, provider, declared, selected):
    from hermes_cli.providers import determine_api_mode

    profile, _ = profile_catalog
    profile.base_url = declared
    assert determine_api_mode(provider, selected, PROFILE_MODEL) == "anthropic_messages"
    assert _profile_pool_runtime(provider, selected)["api_mode"] == "anthropic_messages"


@pytest.mark.parametrize("url_env", [PROFILE_URL_ENV, "TRANSPORT_TEST_URL"])
@pytest.mark.parametrize("literal", ["", "https://old.example.test/api/coding"])
def test_scoped_url_declaration_overrides_literal(
    profile_catalog, scoped_profile_env, monkeypatch, url_env, literal
):
    from hermes_cli.providers import determine_api_mode

    profile, _ = profile_catalog
    profile.env_vars = (PROFILE_KEY_ENV, url_env)
    profile.base_url = literal
    scoped_profile_env[url_env] = " \t" + PROFILE_URL + "/ \n"
    monkeypatch.setenv(url_env, "https://other-scope.example.test/api/coding")
    assert determine_api_mode(PROFILE_NAME, PROFILE_URL) == "anthropic_messages"
    assert _profile_pool_runtime()["api_mode"] == "anthropic_messages"
    if literal:
        assert determine_api_mode(PROFILE_NAME, literal) == "chat_completions"
        assert _profile_pool_runtime(base_url=literal)["api_mode"] == "chat_completions"


def test_process_url_declaration_without_scope(profile_catalog, monkeypatch):
    from agent.secret_scope import reset_secret_scope, set_secret_scope

    profile, _ = profile_catalog
    profile.base_url = ""
    monkeypatch.setenv(PROFILE_URL_ENV, PROFILE_URL + "/")
    token = set_secret_scope(None)
    try:
        assert _profile_pool_runtime()["api_mode"] == "anthropic_messages"
    finally:
        reset_secret_scope(token)


@pytest.mark.parametrize("literal,expected", [(PROFILE_URL, "anthropic_messages"), ("", "chat_completions")])
@pytest.mark.parametrize("missing_value", [None, "", " \t"])
def test_missing_scoped_url_uses_only_literal(
    profile_catalog, scoped_profile_env, monkeypatch, literal, expected, missing_value
):
    from hermes_cli.providers import determine_api_mode

    profile, _ = profile_catalog
    profile.base_url = literal
    if missing_value is not None:
        scoped_profile_env[PROFILE_URL_ENV] = missing_value
    # Neither another scope's URL nor a URL-shaped API key declares this endpoint.
    monkeypatch.setenv(PROFILE_URL_ENV, PROFILE_URL)
    scoped_profile_env[PROFILE_KEY_ENV] = PROFILE_URL
    assert determine_api_mode(PROFILE_NAME, PROFILE_URL) == expected
    assert _profile_pool_runtime()["api_mode"] == expected
    if not literal:
        assert determine_api_mode(PROFILE_NAME, "") == "chat_completions"


def test_catalog_collision_keeps_own_endpoint_transport(profile_catalog):
    from hermes_cli.providers import determine_api_mode, get_provider

    profile, catalog = profile_catalog
    before = _profile_pool_runtime()
    assert before["api_mode"] == "anthropic_messages"
    # Change only catalog presence. Keep profile, selected URL, key and model fixed.
    catalog[PROFILE_NAME] = {
        "name": "Catalog transport test",
        "env": [PROFILE_KEY_ENV],
        "api": PROFILE_URL,
        "models": {PROFILE_MODEL: {"id": PROFILE_MODEL}},
    }
    pdef = get_provider(PROFILE_NAME, allow_network=False)
    assert pdef is not None
    assert pdef.source == "models.dev"
    assert pdef.transport == "openai_chat"
    assert profile.api_mode == "anthropic_messages"
    assert _profile_pool_runtime() == before
    assert determine_api_mode(PROFILE_NAME, PROFILE_URL, PROFILE_MODEL) == "anthropic_messages"
    # Away from the declared endpoint the catalog's native transport still applies.
    foreign = "https://foreign.example.test/v1"
    assert determine_api_mode(PROFILE_NAME, foreign) == "chat_completions"
    assert _profile_pool_runtime(base_url=foreign)["api_mode"] == "chat_completions"


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("https://api.openai.com/v1", "codex_responses"),
        ("https://api.x.ai/v1", "codex_responses"),
        ("https://foreign.example.test/v1", "chat_completions"),
        (PROFILE_URL + "/other", "chat_completions"),
    ],
)
def test_foreign_endpoint_precedence(profile_catalog, endpoint, expected):
    assert _fallback_api_mode(PROFILE_NAME, endpoint, PROFILE_MODEL) == expected
    assert _profile_pool_runtime(base_url=endpoint)["api_mode"] == expected


@pytest.mark.parametrize("endpoint", [PROFILE_URL, "https://api.openai.com/v1", "https://api.x.ai/v1"])
def test_configured_mode_precedes_profile_and_url(profile_catalog, endpoint):
    assert _profile_pool_runtime(base_url=endpoint, api_mode="chat_completions")["api_mode"] == "chat_completions"


@pytest.mark.parametrize("endpoint", ["https://api.openai.com/v1", "https://api.x.ai/v1"])
def test_recognized_own_endpoint_precedes_profile(profile_catalog, endpoint):
    profile, _ = profile_catalog
    profile.base_url = endpoint
    assert _profile_pool_runtime(base_url=endpoint)["api_mode"] == "codex_responses"


@pytest.mark.parametrize("provider,expected", [("openai-api", "codex_responses"), ("deepseek", "chat_completions")])
@pytest.mark.parametrize("catalog_present", [False, True])
def test_overlay_precedes_matching_profile(profile_catalog, provider, expected, catalog_present):
    import providers
    from hermes_cli.providers import determine_api_mode

    profile, catalog = profile_catalog
    profile.name = provider
    providers.register_provider(profile)
    if catalog_present:
        catalog[provider] = {"name": provider, "env": [], "api": PROFILE_URL, "models": {}}
    for endpoint in (PROFILE_URL, "https://foreign.example.test/v1"):
        assert determine_api_mode(provider, endpoint, PROFILE_MODEL) == expected
        assert _profile_pool_runtime(provider, endpoint)["api_mode"] == expected
