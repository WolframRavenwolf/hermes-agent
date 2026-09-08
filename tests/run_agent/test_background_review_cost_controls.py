"""Unit coverage for the background-review aux-model selector + routed digest.

Covers the two behaviors this change adds:
  • _resolve_review_runtime — auto/same-model → not routed (main model, warm
    cache); a configured different model → routed with resolved credentials.
  • _digest_history — compact replay used ONLY on the routed path (recent tail
    verbatim + a digest of older turns), preserving role alternation.

Pure-function / config-driven; no live model calls.
"""
from typing import Any
from unittest.mock import patch

import pytest

from agent import background_review as br


def _msg(role, content, tool_calls=None):
    m = {"role": role, "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


# ---------------------------------------------------------------------------
# _resolve_review_runtime — the aux-model selector
# ---------------------------------------------------------------------------

class _FakeAgent:
    def __init__(self, provider="openai-codex", model="gpt-5.5"):
        self.provider = provider
        self.model = model
        self._credential_pool: Any = None
        self.request_overrides = {}
        self._active_fallback_service_tier_override: str | None = None
        self.max_tokens: int | None = None

    def _current_main_runtime(self):
        return {
            "api_key": "parent-key",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "codex_app_server",
        }


def test_routing_auto_inherits_parent_and_downgrades_codex_app_server():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {"provider": "auto", "model": ""}}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False
    assert rt["provider"] == "openai-codex"
    assert rt["model"] == "gpt-5.5"
    assert rt["api_mode"] == "codex_responses"  # downgraded so agent-loop tools dispatch


def test_routing_to_different_model_marks_routed_and_resolves_credentials():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "google/gemini-3-flash-preview",
    }}}
    fake_rp = {
        "provider": "openrouter", "api_key": "or-key",
        "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions",
        "credential_pool": "routed-pool",
        "request_overrides": {"extra_body": {"store": False}},
        "max_output_tokens": 2048,
    }
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_rp):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is True
    assert rt["provider"] == "openrouter"
    assert rt["model"] == "google/gemini-3-flash-preview"
    assert rt["api_key"] == "or-key"
    assert rt["credential_pool"] == "routed-pool"
    assert rt["request_overrides"] == {"extra_body": {"store": False}}
    assert rt["max_tokens"] == 2048


def test_unrouted_runtime_keeps_parent_pool_and_overrides():
    agent = _FakeAgent()
    agent._credential_pool = "parent-pool"
    agent.request_overrides = {"service_tier": "priority"}
    agent.max_tokens = 4096
    with patch("hermes_cli.config.load_config", return_value={}), patch("hermes_cli.config.load_config_readonly", return_value={}):
        rt = br._resolve_review_runtime(agent)
    assert rt["credential_pool"] == "parent-pool"
    assert rt["request_overrides"] == {"service_tier": "priority"}
    assert rt["max_tokens"] == 4096


def test_unrouted_runtime_applies_active_fallback_tier_policy_without_mutation():
    agent = _FakeAgent()
    agent.request_overrides = {
        "service_tier": "priority",
        "extra_body": {"speed": "fast", "store": False},
        "custom": {"trace": True},
    }
    agent._active_fallback_service_tier_override = "normal"

    with (
        patch("hermes_cli.config.load_config", return_value={}),
        patch("hermes_cli.config.load_config_readonly", return_value={}),
    ):
        rt = br._resolve_review_runtime(agent)

    assert rt["request_overrides"] == {
        "extra_body": {"store": False},
        "custom": {"trace": True},
    }
    assert agent.request_overrides["service_tier"] == "priority"
    assert agent.request_overrides["extra_body"]["speed"] == "fast"


def test_routed_review_keeps_its_own_tier_policy():
    agent = _FakeAgent()
    agent.request_overrides = {"speed": "fast"}
    agent._active_fallback_service_tier_override = "normal"
    routed = {"request_overrides": {"extra_body": {"service_tier": "flex"}}}
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=routed):
        rt = br._resolve_review_runtime(agent, {
            "provider": "openrouter", "model": "review-model",
        })
    assert rt["routed"] is True
    assert rt["request_overrides"] == routed["request_overrides"]
    assert agent.request_overrides == {"speed": "fast"}


def test_failed_review_routing_inherits_active_route_tier_policy():
    agent = _FakeAgent()
    agent.request_overrides = {"speed": "fast", "extra_body": {"service_tier": "flex"}}
    agent._active_fallback_service_tier_override = "normal"
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=RuntimeError("unavailable")):
        rt = br._resolve_review_runtime(agent, {
            "provider": "openrouter", "model": "review-model",
        })
    assert rt["routed"] is False
    assert rt["request_overrides"] == {"extra_body": {}}
    assert agent.request_overrides["extra_body"]["service_tier"] == "flex"


def test_routing_same_model_as_parent_is_not_routed():
    agent = _FakeAgent(provider="openrouter", model="anthropic/claude-opus-4.8")
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "anthropic/claude-opus-4.8",
    }}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False  # same model/provider → keep full-replay path


def test_routing_resolution_failure_falls_back_to_parent():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "google/gemini-3-flash-preview",
    }}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               side_effect=RuntimeError("boom")):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False
    assert rt["provider"] == "openai-codex"


# ---------------------------------------------------------------------------
# _digest_history — routed-path compact replay
# ---------------------------------------------------------------------------

def test_digest_under_tail_returns_full():
    msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
    assert br._digest_history(msgs, tail=24) == msgs


def test_digest_collapses_old_keeps_tail_verbatim():
    msgs = []
    for i in range(60):
        msgs.append(_msg("user", f"u{i} " + "x" * 50))
        msgs.append(_msg("assistant", f"a{i} " + "y" * 50))
    out = br._digest_history(msgs, tail=10)
    # First message is the synthetic digest (user role → alternation preserved).
    assert out[0]["role"] == "user"
    assert out[0]["content"].startswith("[Earlier conversation digest")
    # Recent tail preserved verbatim.
    assert out[-1] == msgs[-1]
    assert len(out) == 11  # 1 digest + 10 tail


def test_digest_does_not_open_tail_on_a_tool_message():
    msgs = []
    for i in range(40):
        msgs.append(_msg("user", "u" + "x" * 50))
        msgs.append(_msg("assistant", "", tool_calls=[
            {"function": {"name": "terminal", "arguments": "{}"}}]))
        msgs.append({"role": "tool", "content": "result " + "w" * 50})
    out = br._digest_history(msgs, tail=2)
    # The verbatim tail (after the digest) must not begin on a bare tool message.
    assert out[1]["role"] != "tool"


def test_digest_records_tool_names_in_arc():
    old = [
        _msg("user", "do the thing"),
        _msg("assistant", "", tool_calls=[
            {"function": {"name": "skill_view", "arguments": "{}"}},
            {"function": {"name": "patch", "arguments": "{}"}}]),
    ]
    msgs = old + [_msg("user", f"tail{i}") for i in range(30)]
    out = br._digest_history(msgs, tail=10)
    digest = out[0]["content"]
    assert "USER: do the thing" in digest
    assert "tools: skill_view, patch" in digest


# ---------------------------------------------------------------------------
# Cost / configurability controls (issue #87250)
# ---------------------------------------------------------------------------

def test_enabled_defaults_true():
    with patch("hermes_cli.config.load_config_readonly", return_value={}):
        assert br.is_background_review_enabled() is True


def test_enabled_false_disables_automatic_review():
    cfg = {"auxiliary": {"background_review": {"enabled": False}}}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        assert br.is_background_review_enabled() is False


@pytest.fixture
def real_review_runtime(monkeypatch):
    """Keep constructor/config merge/transport real; replace config and API I/O."""
    from unittest.mock import MagicMock
    from tests.run_agent.test_background_review import _bare_agent

    parent: Any = _bare_agent()
    parent.provider = "custom"
    parent.model = "gpt-5.5"
    parent.base_url = "https://tier-test.example.test/v1"
    parent.api_key = "fixture-key"
    parent.api_mode = "chat_completions"
    parent.enabled_toolsets = []
    parent.request_overrides = {"extra_body": {"service_tier": "priority", "store": False}}
    parent._active_fallback_service_tier_override = "normal"
    parent._current_main_runtime = lambda: {
        "api_key": parent.api_key,
        "base_url": parent.base_url,
        "api_mode": parent.api_mode,
    }
    cfg = {
        "agent": {"environment_probe": False},
        "custom_providers": [{
            "name": "tier-test", "model": parent.model,
            "base_url": parent.base_url,
            "extra_body": {"service_tier": "priority", "provider_marker": "retained"},
        }, {
            "name": "review-tier", "model": "gpt-4.1",
            "base_url": "https://review-tier.example.test/v1",
            "extra_body": {"service_tier": "flex", "provider_marker": "routed"},
        }],
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)
    monkeypatch.setattr("run_agent.OpenAI", lambda **kwargs: MagicMock(**kwargs))
    return parent


@pytest.mark.parametrize("tier_override, expected_tier", [("normal", None), (None, "priority")])
@pytest.mark.parametrize("restore_primary", [False, True])
def test_real_inherited_fork_keeps_normal_tier_after_provider_config_reload(
    real_review_runtime, tier_override, expected_tier, restore_primary,
):
    """Check the actual request, both on init and after native fallback/restore."""
    from copy import deepcopy
    from unittest.mock import MagicMock

    parent = real_review_runtime
    parent._active_fallback_service_tier_override = tier_override
    before = deepcopy(parent.request_overrides)
    fork, runtime, routed = br.build_cache_parity_fork(parent, {}, max_iterations=1)
    try:
        assert routed is False
        expected_overrides: dict[str, Any] = {"extra_body": {"store": False}}
        if tier_override is None:
            expected_overrides["extra_body"]["service_tier"] = "priority"
        assert runtime["request_overrides"] == expected_overrides
        # The marker proves that the real constructor reloaded provider data.
        assert fork.request_overrides["extra_body"]["provider_marker"] == "retained"
        if restore_primary:
            # Activate a real subsequent fallback; don't merely set the restore flag.
            fork._fallback_chain = [{
                "provider": "custom", "model": "gpt-4.1",
                "base_url": "https://later-tier.example.test/v1",
                "api_key": "later-fixture-key", "api_mode": "chat_completions",
            }]
            client = MagicMock(
                api_key="later-fixture-key", base_url="https://later-tier.example.test/v1",
                _custom_headers={}, default_headers={},
            )
            with patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, "gpt-4.1")):
                assert fork._try_activate_fallback() is True
            assert fork.model == "gpt-4.1"
            assert fork._active_fallback_service_tier_override is None
            assert fork._restore_primary_runtime() is True
            assert fork.model == parent.model
            assert fork.base_url == parent.base_url
        wire = fork._build_api_kwargs([{"role": "user", "content": "fixture"}])
        assert (wire.get("extra_body") or {}).get("service_tier") == expected_tier
        assert wire["extra_body"]["provider_marker"] == "retained"
        assert wire["extra_body"]["store"] is False
        assert parent.request_overrides == before
        assert parent._active_fallback_service_tier_override == tier_override
    finally:
        fork.shutdown_memory_provider()
        fork.close()


def test_real_routed_fork_keeps_its_own_configured_tier(real_review_runtime):
    from copy import deepcopy

    parent = real_review_runtime
    before = deepcopy(parent.request_overrides)
    fork, _, routed = br.build_cache_parity_fork(parent, {
        "provider": "custom", "model": "gpt-4.1",
        "base_url": "https://review-tier.example.test/v1", "api_key": "review-fixture-key",
    }, max_iterations=1)
    try:
        assert routed is True
        assert fork.model == "gpt-4.1"
        assert fork.base_url == "https://review-tier.example.test/v1"
        wire = fork._build_api_kwargs([{"role": "user", "content": "fixture"}])
        assert wire["extra_body"]["service_tier"] == "flex"
        assert wire["extra_body"]["provider_marker"] == "routed"
        assert parent.request_overrides == before
        assert parent._active_fallback_service_tier_override == "normal"
    finally:
        fork.shutdown_memory_provider()
        fork.close()


@pytest.mark.parametrize("parent_tier, child_tier, expected_tier", [
    ("normal", None, "priority"), (None, "normal", None),
])
def test_real_fork_preserves_its_own_init_fallback_tier(
    real_review_runtime, parent_tier, child_tier, expected_tier,
):
    from copy import deepcopy
    from functools import partial
    from unittest.mock import MagicMock
    from run_agent import AIAgent

    parent = real_review_runtime
    parent.provider = "alibaba-coding-plan"
    parent.model = "qwen3.6-plus"
    parent.api_key = parent.base_url = ""
    parent._active_fallback_service_tier_override = parent_tier
    before = deepcopy(parent.request_overrides)
    fallback = {
        "provider": "custom", "model": "gpt-5.5",
        "service_tier_override": child_tier,
    }
    client = MagicMock(
        api_key="fixture-key", base_url="https://tier-test.example.test/v1",
        _custom_headers={}, default_headers={}, _default_headers={},
    )

    def resolve_client(provider, **kwargs):
        if provider == "alibaba-coding-plan":
            return None, None
        assert provider == "custom"
        return client, "gpt-5.5"

    # Supply a fallback argument to the REAL constructor (the fork normally
    # supplies none). No fake instance, constructor body, merge or restore.
    with (
        patch("run_agent.AIAgent", new=partial(AIAgent, fallback_model=fallback)),
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve_client),
    ):
        fork, _, routed = br.build_cache_parity_fork(parent, {}, max_iterations=1)
    try:
        assert routed is False
        assert fork._fallback_activated is True
        for restored in (False, True):
            if restored:
                assert fork._restore_primary_runtime() is True
            assert fork.provider == "custom"
            assert fork.model == "gpt-5.5"
            wire = fork._build_api_kwargs([{"role": "user", "content": "fixture"}])
            assert (wire.get("extra_body") or {}).get("service_tier") == expected_tier
            assert wire["extra_body"]["provider_marker"] == "retained"
        assert parent.request_overrides == before
        assert parent._active_fallback_service_tier_override == parent_tier
        assert isinstance(fork, AIAgent)
    finally:
        fork.shutdown_memory_provider()
        fork.close()
