from __future__ import annotations

import asyncio
import copy
import json
import threading
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import gateway.run as gateway_run
from gateway.platforms.base import MessageEvent, MessageType, Platform, SessionSource
from gateway.run import GatewayRunner
from gateway.session import GatewayConfig, SessionStore
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.commands import resolve_command


_MANUAL_KEY = "manual_fallback_index"


def _source(
    *, thread_id: str = "thread-a", profile: str | None = None
) -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="thread",
        thread_id=thread_id,
        user_id="owner-1",
        profile=profile,
    )


def _event(command: str, *, source: SessionSource | None = None) -> MessageEvent:
    return MessageEvent(
        text=command,
        source=source or _source(),
        message_type=MessageType.TEXT,
    )


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionStore:
    import hermes_state

    def _raise():
        raise RuntimeError("SQLite disabled in test")

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)
    return SessionStore(tmp_path / "sessions", GatewayConfig())


def _sqlite_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionStore:
    import hermes_state

    real_session_db = hermes_state.SessionDB
    store = _store(tmp_path, monkeypatch)
    store._db = real_session_db(db_path=tmp_path / "state.db")
    return store


def _runner(
    store: SessionStore,
    chain: list[dict],
) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = store
    runner._sessions = {}
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    runner._running_agents = {}
    runner._fallback_model = copy.deepcopy(chain)
    runner._refresh_fallback_model = lambda: copy.deepcopy(chain)
    runner._normalize_source_for_session_key = lambda source: source

    async def _run_in_executor(func, *args):
        return func(*args)

    runner._run_in_executor_with_context = _run_in_executor
    runner._evict_cached_agent = MagicMock()
    return runner


def _chain(*, secret: str = "secret-sentinel") -> list[dict]:
    return [
        {
            "provider": "openrouter",
            "model": "google/gemini-3.1-pro-preview",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": secret,
            "api_mode": "chat_completions",
            "service_tier_override": "normal",
        },
        {
            "provider": "anthropic",
            "model": "claude-opus-4-6",
        },
    ]


def test_fallback_command_is_gateway_registered_and_busy_rejected():
    command = resolve_command("fallback")

    assert command is not None
    assert command.gateway_only is True
    assert command.busy_policy == "reject"
    assert command.args_hint == "[on|off|status]"


def test_session_store_can_remove_metadata_persistently(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    source = _source()
    entry = store.get_or_create_session(source)
    assert store.set_session_metadata(entry.session_key, _MANUAL_KEY, 0)

    assert store.delete_session_metadata(entry.session_key, _MANUAL_KEY) is True
    assert store.get_session_metadata(entry.session_key, _MANUAL_KEY) is None
    assert store.delete_session_metadata(entry.session_key, _MANUAL_KEY) is False

    reloaded = SessionStore(store.sessions_dir, GatewayConfig())
    assert reloaded.get_session_metadata(entry.session_key, _MANUAL_KEY) is None


def test_new_session_reset_clears_manual_fallback_marker(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(_source())
    store.set_session_metadata(entry.session_key, _MANUAL_KEY, 0)

    replacement = store.reset_session(entry.session_key)

    assert replacement is not None
    assert replacement.session_id != entry.session_id
    assert store.get_session_metadata(entry.session_key, _MANUAL_KEY) is None
    reloaded = SessionStore(store.sessions_dir, GatewayConfig())
    assert reloaded.get_session_metadata(entry.session_key, _MANUAL_KEY) is None


@pytest.mark.asyncio
async def test_fallback_on_validates_entry_persists_only_index_and_evicts_cache(
    tmp_path, monkeypatch
):
    secret = "never-persist-this-secret"
    chain = _chain(secret=secret)
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, chain)
    source = _source()
    entry = store.get_or_create_session(source)
    resolver = MagicMock(
        return_value=(
            chain[0]["model"],
            {
                "provider": chain[0]["provider"],
                "api_key": "resolved-secret",
                "base_url": chain[0]["base_url"],
                "api_mode": "chat_completions",
            },
        )
    )
    monkeypatch.setattr(gateway_run, "_resolve_fallback_entry_agent_kwargs", resolver)

    response = await runner._handle_fallback_command(
        _event("/fallback on", source=source)
    )

    assert "Manual fallback: ON" in response
    assert chain[0]["model"] in response
    assert chain[0]["provider"] in response
    assert secret not in response
    assert "resolved-secret" not in response
    assert store.get_session_metadata(entry.session_key, _MANUAL_KEY) == 0
    resolver.assert_called_once_with(chain[0])
    runner._evict_cached_agent.assert_called_once_with(entry.session_key)

    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in store.sessions_dir.rglob("*")
        if path.is_file()
    )
    assert secret not in persisted
    assert "resolved-secret" not in persisted
    assert json.dumps({_MANUAL_KEY: 0})[1:-1] in persisted


@pytest.mark.parametrize(("action", "expected_marker"), [("on", 0), ("off", None)])
@pytest.mark.asyncio
async def test_fallback_routing_claim_survives_cancellation_until_store_finishes(
    action, expected_marker, tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, _chain())
    source = _source()
    entry = store.get_or_create_session(source)
    if action == "off":
        assert store.set_session_metadata(entry.session_key, _MANUAL_KEY, 0)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_fallback_entry_agent_kwargs",
        lambda candidate: (candidate["model"], {"provider": candidate["provider"]}),
    )

    method_name = (
        "set_session_metadata" if action == "on" else "delete_session_metadata"
    )
    real_method = getattr(store, method_name)
    entered = threading.Event()
    release = threading.Event()

    def _blocked_store(*args, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return real_method(*args, **kwargs)

    monkeypatch.setattr(store, method_name, _blocked_store)
    task = asyncio.create_task(
        runner._handle_fallback_command(
            _event(f"/fallback {action}", source=source)
        )
    )
    assert await asyncio.to_thread(entered.wait, 2)

    task.cancel()
    await asyncio.sleep(0)
    try:
        assert task.done()
        assert runner._is_routing_mutation_active(entry.session_key)
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if not runner._is_routing_mutation_active(entry.session_key):
            break
        await asyncio.sleep(0.01)
    assert not runner._is_routing_mutation_active(entry.session_key)
    assert store.get_session_metadata(entry.session_key, _MANUAL_KEY) == expected_marker
    runner._evict_cached_agent.assert_called_once_with(entry.session_key)


@pytest.mark.asyncio
async def test_fallback_on_without_configured_chain_is_a_noop(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, [])
    source = _source()
    entry = store.get_or_create_session(source)

    response = await runner._handle_fallback_command(
        _event("/fallback on", source=source)
    )

    assert "No fallback" in response
    assert store.get_session_metadata(entry.session_key, _MANUAL_KEY) is None
    runner._evict_cached_agent.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_on_does_not_echo_resolver_secrets_or_mutate_state(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, _chain())
    source = _source()
    entry = store.get_or_create_session(source)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_fallback_entry_agent_kwargs",
        MagicMock(side_effect=RuntimeError("credential secret-leak-sentinel failed")),
    )

    response = await runner._handle_fallback_command(
        _event("/fallback on", source=source)
    )

    assert "could not be enabled" in response
    assert "secret-leak-sentinel" not in response
    assert store.get_session_metadata(entry.session_key, _MANUAL_KEY) is None
    runner._evict_cached_agent.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_rejects_unknown_argument_without_mutation(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, _chain())
    source = _source()
    entry = store.get_or_create_session(source)

    response = await runner._handle_fallback_command(
        _event("/fallback maybe", source=source)
    )

    assert "Usage: /fallback [on|off|status]" in response
    assert store.get_session_metadata(entry.session_key, _MANUAL_KEY) is None
    runner._evict_cached_agent.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_off_removes_marker_but_preserves_model_override(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, _chain())
    source = _source()
    entry = store.get_or_create_session(source)
    store.set_session_metadata(entry.session_key, _MANUAL_KEY, 0)
    store.set_model_override(
        entry.session_key,
        {"model": "openai/gpt-5.5", "provider": "openrouter"},
    )

    response = await runner._handle_fallback_command(
        _event("/fallback off", source=source)
    )

    assert "Manual fallback: OFF" in response
    assert store.get_session_metadata(entry.session_key, _MANUAL_KEY) is None
    assert store.get_model_override(entry.session_key) == {
        "model": "openai/gpt-5.5",
        "provider": "openrouter",
    }
    runner._evict_cached_agent.assert_called_once_with(entry.session_key)


def test_manual_fallback_route_survives_store_reload_and_is_session_isolated(
    tmp_path, monkeypatch
):
    chain = _chain()
    store = _store(tmp_path, monkeypatch)
    source_a = _source(thread_id="thread-a")
    source_b = _source(thread_id="thread-b")
    entry_a = store.get_or_create_session(source_a)
    entry_b = store.get_or_create_session(source_b)
    store.set_session_metadata(entry_a.session_key, _MANUAL_KEY, 0)

    reloaded = SessionStore(store.sessions_dir, GatewayConfig())
    runner = _runner(reloaded, chain)
    resolver = MagicMock(
        return_value=(
            chain[0]["model"],
            {
                "provider": chain[0]["provider"],
                "api_key": "resolved-secret",
                "base_url": chain[0]["base_url"],
                "api_mode": "chat_completions",
            },
        )
    )
    monkeypatch.setattr(gateway_run, "_resolve_fallback_entry_agent_kwargs", resolver)

    model, runtime = runner._resolve_session_agent_runtime(
        source=source_a,
        session_key=entry_a.session_key,
        user_config={},
    )

    assert model == chain[0]["model"]
    assert runtime["provider"] == chain[0]["provider"]
    assert runtime["fallback_service_tier_override"] == "normal"
    assert runtime["manual_fallback_index"] == 0
    assert runtime["fallback_model"] == chain[1:]
    assert runner._manual_fallback_tail_offset(runtime) == 1
    assert reloaded.get_session_metadata(entry_b.session_key, _MANUAL_KEY) is None


def test_cached_agent_refresh_uses_the_manual_route_snapshot():
    chain = _chain()
    runner = object.__new__(GatewayRunner)
    runner._refresh_fallback_model = MagicMock(return_value=list(reversed(chain)))
    runtime = {"manual_fallback_index": 0, "fallback_model": chain[1:]}

    refreshed = runner._fallback_chain_for_runtime(runtime)

    assert refreshed == chain[1:]
    runner._refresh_fallback_model.assert_not_called()


def test_stale_manual_fallback_marker_fails_closed_with_recovery_hint(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    source = _source()
    entry = store.get_or_create_session(source)
    store.set_session_metadata(entry.session_key, _MANUAL_KEY, 4)
    runner = _runner(store, _chain())

    with pytest.raises(RuntimeError, match=r"/fallback off"):
        runner._resolve_session_agent_runtime(
            source=source,
            session_key=entry.session_key,
            user_config={},
        )


@pytest.mark.asyncio
async def test_fallback_status_distinguishes_manual_and_automatic_state(
    tmp_path, monkeypatch
):
    chain = _chain()
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, chain)
    source = _source()
    entry = store.get_or_create_session(source)
    store.set_session_metadata(entry.session_key, _MANUAL_KEY, 0)
    automatic_agent = SimpleNamespace(
        _fallback_activated=True,
        model=chain[1]["model"],
        provider=chain[1]["provider"],
    )
    runner._agent_cache[entry.session_key] = (automatic_agent, "sig", 0, entry.session_id)

    response = await runner._handle_fallback_command(
        _event("/fallback status", source=source)
    )

    assert "Manual fallback: ON" in response
    assert "Automatic fallback: ACTIVE" in response
    assert chain[1]["model"] in response
    assert "api_key" not in response


def test_runtime_signature_includes_manual_fallback_policy():
    base_runtime = {
        "provider": "openrouter",
        "api_key": "secret",
        "base_url": "https://openrouter.ai/api/v1",
        "api_mode": "chat_completions",
    }
    normal_runtime = dict(base_runtime, fallback_service_tier_override="normal")

    base = GatewayRunner._agent_config_signature("same-model", base_runtime, [], "")
    manual = GatewayRunner._agent_config_signature("same-model", normal_runtime, [], "")

    assert manual != base


def test_fallback_entry_runtime_uses_configured_endpoint_key_and_api_mode(monkeypatch):
    entry = _chain()[0]
    runtime_config = {"providers": {}}
    resolved_runtime = {
        "provider": "openrouter",
        "requested_provider": "openrouter",
        "api_key": "resolved-secret",
        "base_url": "https://resolved.example/v1",
        "api_mode": "codex_responses",
    }
    runtime_resolver = MagicMock(return_value=resolved_runtime)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        runtime_resolver,
    )
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: runtime_config,
    )
    monkeypatch.setattr(
        "hermes_cli.fallback_config.resolve_entry_api_key",
        lambda candidate: "entry-secret",
    )

    model, runtime = gateway_run._resolve_fallback_entry_agent_kwargs(entry)

    runtime_resolver.assert_called_once_with(
        "openrouter",
        target_model=entry["model"],
        explicit_base_url=entry["base_url"],
        explicit_api_key="entry-secret",
        runtime_config=runtime_config,
    )
    assert model == entry["model"]
    assert runtime["api_mode"] == entry["api_mode"]
    assert runtime["api_key"] == "resolved-secret"


def test_fallback_entry_runtime_rejects_unknown_api_mode(monkeypatch):
    entry = dict(_chain()[0], api_mode="unknown_transport")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        MagicMock(return_value={"provider": "openrouter"}),
    )

    with pytest.raises(RuntimeError, match="api_mode"):
        gateway_run._resolve_fallback_entry_agent_kwargs(entry)


@pytest.mark.parametrize(
    ("api_mode", "requested_provider"),
    [
        ("bedrock_converse", "profile-provider"),
        ("codex_app_server", "profile-provider"),
        ("bedrock_converse", "bedrock"),
        ("codex_app_server", "openai"),
    ],
)
def test_fallback_entry_runtime_rejects_foreign_native_api_mode(
    api_mode, requested_provider, monkeypatch
):
    entry = dict(_chain()[0], provider=requested_provider, api_mode=api_mode)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        MagicMock(
            return_value={
                "provider": "custom",
                "requested_provider": requested_provider,
                "api_key": "usable-fixture-credential",
                "base_url": "https://fallback.example/v1",
                "api_mode": "chat_completions",
            }
        ),
    )

    with pytest.raises(RuntimeError, match="api_mode"):
        gateway_run._resolve_fallback_entry_agent_kwargs(entry)


def test_runtime_projection_marks_header_auth_without_copying_headers(monkeypatch):
    from hermes_cli import runtime_provider

    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        MagicMock(
            return_value={
                "provider": "custom",
                "requested_provider": "profile-provider",
                "api_key": "no-key-required",
                "base_url": "https://fallback.example/v1",
                "api_mode": "chat_completions",
                "extra_headers": {"Authorization": "opaque-fixture-credential"},
            }
        ),
    )

    runtime = gateway_run._resolve_runtime_agent_kwargs_for_provider(
        "profile-provider"
    )

    assert runtime["has_header_auth"] is True
    assert "extra_headers" not in runtime


def test_fallback_entry_runtime_accepts_header_authenticated_provider(monkeypatch):
    entry = {"provider": "profile-provider", "model": "profile-model"}
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        MagicMock(
            return_value={
                "provider": "custom",
                "requested_provider": "profile-provider",
                "api_key": "no-key-required",
                "base_url": "https://fallback.example/v1",
                "api_mode": "chat_completions",
                "has_header_auth": True,
            }
        ),
    )

    model, runtime = gateway_run._resolve_fallback_entry_agent_kwargs(entry)

    assert model == "profile-model"
    assert runtime["has_header_auth"] is True


def test_fallback_entry_runtime_accepts_callable_token_provider(monkeypatch):
    entry = {"provider": "azure-entra", "model": "deployment-name"}

    def token_provider():
        return "opaque-fixture-token"

    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        MagicMock(
            return_value={
                "provider": "custom",
                "requested_provider": "azure-entra",
                "api_key": token_provider,
                "base_url": "https://fallback.example/v1",
                "api_mode": "chat_completions",
            }
        ),
    )

    model, runtime = gateway_run._resolve_fallback_entry_agent_kwargs(entry)

    assert model == "deployment-name"
    assert runtime["api_key"] is token_provider


def test_fallback_entry_runtime_rejects_missing_usable_api_key(monkeypatch):
    entry = _chain()[0]
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        MagicMock(
            return_value={
                "provider": "openrouter",
                "api_key": None,
                "base_url": "https://openrouter.ai/api/v1",
                "api_mode": "chat_completions",
            }
        ),
    )

    with pytest.raises(RuntimeError, match="API key"):
        gateway_run._resolve_fallback_entry_agent_kwargs(entry)


def test_fallback_entry_runtime_rejects_no_key_required_sentinel(monkeypatch):
    entry = _chain()[0]
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        MagicMock(
            return_value={
                "provider": "custom",
                "api_key": "no-key-required",
                "base_url": "https://fallback.example/v1",
                "api_mode": "chat_completions",
            }
        ),
    )

    with pytest.raises(RuntimeError, match="API key"):
        gateway_run._resolve_fallback_entry_agent_kwargs(entry)


@pytest.mark.parametrize(
    ("runtime_field", "unresolved_value", "error_match"),
    [
        ("api_key", "${MISSING_FALLBACK_KEY}", "API key"),
        ("base_url", "https://${MISSING_FALLBACK_HOST}/v1", "base URL"),
    ],
)
def test_fallback_entry_runtime_rejects_unresolved_env_refs(
    runtime_field, unresolved_value, error_match, monkeypatch
):
    entry = _chain()[0]
    runtime = {
        "provider": "openrouter",
        "api_key": "usable-fixture-credential",
        "base_url": "https://openrouter.ai/api/v1",
        "api_mode": "chat_completions",
    }
    runtime[runtime_field] = unresolved_value
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        MagicMock(return_value=runtime),
    )

    with pytest.raises(RuntimeError, match=error_match):
        gateway_run._resolve_fallback_entry_agent_kwargs(entry)


def test_fallback_entry_runtime_rejects_malformed_http_endpoint(monkeypatch):
    entry = dict(_chain()[0], base_url="not-a-url")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        MagicMock(
            return_value={
                "provider": "custom",
                "api_key": "usable-fixture-credential",
                "base_url": "not-a-url",
                "api_mode": "chat_completions",
            }
        ),
    )

    with pytest.raises(RuntimeError, match="base URL"):
        gateway_run._resolve_fallback_entry_agent_kwargs(entry)


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://fallback.example:notaport/v1",
        "https://user:opaque-password@fallback.example/v1",
        "https://fallback.example/v1?token=opaque-query-value",
        "https://fallback.example/v1#opaque-fragment",
    ],
)
def test_fallback_entry_runtime_rejects_non_persistable_http_endpoint(
    bad_url, monkeypatch
):
    entry = dict(_chain()[0], base_url=bad_url)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        MagicMock(
            return_value={
                "provider": "custom",
                "api_key": "usable-fixture-credential",
                "base_url": bad_url,
                "api_mode": "chat_completions",
            }
        ),
    )

    with pytest.raises(RuntimeError, match="base URL"):
        gateway_run._resolve_fallback_entry_agent_kwargs(entry)


@pytest.mark.parametrize(
    "configured_key",
    [
        {"api_key": "${MISSING_FALLBACK_KEY}"},
        {"key_env": "MISSING_FALLBACK_KEY"},
    ],
)
def test_fallback_entry_runtime_rejects_unresolved_configured_key_before_resolution(
    configured_key, monkeypatch
):
    entry = dict(_chain()[0])
    entry.pop("api_key")
    entry.update(configured_key)
    resolver = MagicMock(
        return_value={
            "provider": "openrouter",
            "api_key": "ambient-provider-key-that-must-not-win",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        }
    )
    monkeypatch.delenv("MISSING_FALLBACK_KEY", raising=False)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        resolver,
    )

    with pytest.raises(RuntimeError, match="API key"):
        gateway_run._resolve_fallback_entry_agent_kwargs(entry)
    resolver.assert_not_called()


def test_fallback_entry_runtime_uses_profile_expanded_named_provider_config(
    monkeypatch
):
    from hermes_cli import runtime_provider

    entry = {"provider": "profile-provider", "model": "profile-model"}
    profile_config = {
        "providers": {
            "profile-provider": {
                "name": "profile-provider",
                "base_url": "https://profile.example/v1",
                "api_key": "profile-scoped-key",
            }
        }
    }
    global_config = {
        "providers": {
            "profile-provider": {
                "name": "profile-provider",
                "base_url": "https://global.example/v1",
                "api_key": "global-key-that-must-not-win",
            }
        }
    }
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: copy.deepcopy(profile_config),
    )
    monkeypatch.setattr(runtime_provider, "load_config", lambda: global_config)

    model, runtime = gateway_run._resolve_fallback_entry_agent_kwargs(entry)

    assert model == "profile-model"
    assert runtime["base_url"] == "https://profile.example/v1"
    assert runtime["api_key"] == "profile-scoped-key"


def test_gateway_runtime_config_expands_named_provider_from_active_secret_scope(
    monkeypatch,
):
    from agent import secret_scope

    raw_config = {
        "providers": {
            "profile-provider": {
                "name": "profile-provider",
                "base_url": "${PROFILE_URL}",
                "api_key": "${PROFILE_KEY}",
            }
        }
    }
    profile_secrets = {
        "PROFILE_URL": "https://profile.example/v1",
        "PROFILE_KEY": "profile-scoped-key",
    }
    monkeypatch.setenv("PROFILE_URL", "https://global.example/v1")
    monkeypatch.setenv("PROFILE_KEY", "global-key-that-must-not-win")
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: copy.deepcopy(raw_config),
    )
    monkeypatch.setattr(
        secret_scope,
        "get_secret",
        lambda name: profile_secrets.get(name),
    )

    expanded = gateway_run._load_gateway_runtime_config()

    provider = expanded["providers"]["profile-provider"]
    assert provider["base_url"] == "https://profile.example/v1"
    assert provider["api_key"] == "profile-scoped-key"


def test_fallback_display_label_strictly_redacts_url_credentials():
    from gateway.slash_commands import _fallback_display_label

    label = _fallback_display_label(
        "https://user:opaque-password@host.example/v1?token=opaque-query-value",
        "fallback",
    )

    assert "opaque-password" not in label
    assert "opaque-query-value" not in label


def test_aiagent_forwards_manual_fallback_policy_to_initializer(monkeypatch):
    from run_agent import AIAgent

    initializer = MagicMock()
    monkeypatch.setattr("agent.agent_init.init_agent", initializer)

    AIAgent(
        model="google/gemini-3.1-pro-preview",
        fallback_service_tier_override="normal",
    )

    assert initializer.call_args.kwargs["fallback_service_tier_override"] == "normal"


def test_manual_fallback_parameter_preserves_existing_positional_signatures():
    import inspect

    from agent.agent_init import init_agent
    from run_agent import AIAgent

    expected_tail = [
        "fallback_model",
        "credential_pool",
        "checkpoints_enabled",
        "checkpoint_max_snapshots",
        "checkpoint_max_total_size_mb",
        "checkpoint_max_file_size_mb",
        "pass_session_id",
        "requested_provider",
        "fallback_service_tier_override",
    ]

    assert list(inspect.signature(AIAgent.__init__).parameters)[-9:] == expected_tail
    assert list(inspect.signature(init_agent).parameters)[-9:] == expected_tail


def test_manual_runtime_metadata_is_filtered_before_aiagent_construction():
    import inspect

    from run_agent import AIAgent

    runtime = {
        "provider": "custom",
        "api_key": "usable-fixture-credential",
        "fallback_model": [],
        "manual_fallback_index": 0,
        "has_header_auth": True,
    }

    projected = gateway_run._runtime_kwargs_for_agent_constructor(runtime)

    assert "manual_fallback_index" not in projected
    assert "has_header_auth" not in projected
    assert projected["fallback_model"] == []
    assert runtime["manual_fallback_index"] == 0
    inspect.signature(AIAgent.__init__).bind(None, model="fixture-model", **projected)


@pytest.mark.parametrize("store_factory", [SimpleNamespace, MagicMock])
def test_session_runtime_ignores_store_without_real_metadata_reader(
    monkeypatch, store_factory
):
    store = store_factory()
    runner = _runner(store, [])
    runner._refresh_fallback_model = MagicMock(return_value=[])
    runner._rehydrate_session_model_override = MagicMock()
    runner._peek_session_state = MagicMock(return_value=None)
    state = SimpleNamespace(conversation=SimpleNamespace(last_resolved_model=""))
    runner._session_state = MagicMock(return_value=state)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_gateway_model",
        MagicMock(return_value="primary-model"),
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        MagicMock(return_value={"provider": "openrouter", "api_key": "test-key"}),
    )

    model, runtime = runner._resolve_session_agent_runtime(session_key="session-key")

    assert model == "primary-model"
    assert runtime["provider"] == "openrouter"
    runner._refresh_fallback_model.assert_not_called()


def test_turn_config_preserves_manual_policy_through_gateway_agent_seam(monkeypatch):
    from agent.chat_completion_helpers import build_api_kwargs
    from run_agent import AIAgent

    runner = object.__new__(GatewayRunner)
    runner._service_tier = "priority"
    route = runner._resolve_turn_agent_config(
        "hello",
        "gpt-5.4",
        {
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "api_key": "test-key",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
            "fallback_service_tier_override": "normal",
        },
    )

    assert route["runtime"]["fallback_service_tier_override"] == "normal"
    initializer = MagicMock()
    monkeypatch.setattr("agent.agent_init.init_agent", initializer)
    AIAgent(
        model=route["model"],
        request_overrides=route["request_overrides"],
        **route["runtime"],
    )
    active_policy = initializer.call_args.kwargs[
        "fallback_service_tier_override"
    ]
    assert active_policy == "normal"

    class _CaptureTransport:
        def build_kwargs(self, **kwargs):
            return kwargs

    wire_agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="openrouter",
        model=route["model"],
        session_id="manual-fallback-wire-test",
        tools=[],
        max_tokens=1024,
        reasoning_config=None,
        request_overrides=route["request_overrides"],
        _active_fallback_service_tier_override=active_policy,
        base_url="https://openrouter.ai/api/v1",
        _base_url_lower="https://openrouter.ai/api/v1",
        _base_url_hostname="openrouter.ai",
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
        _is_openrouter_url=lambda: True,
        _qwen_prepare_chat_messages=lambda messages: messages,
        _qwen_prepare_chat_messages_inplace=lambda messages: messages,
        _codex_reasoning_replay_enabled=True,
    )
    monkeypatch.setattr("providers.get_provider_profile", lambda _provider: None)
    monkeypatch.setattr(
        "agent.chat_completion_helpers._provider_preferences_for_agent",
        lambda _agent: None,
    )
    wire_kwargs = build_api_kwargs(
        wire_agent,
        [{"role": "user", "content": "hi"}],
    )
    assert "service_tier" not in wire_kwargs["request_overrides"]


@pytest.mark.asyncio
async def test_fallback_on_rolls_back_when_primary_routing_write_fails(
    tmp_path, monkeypatch
):
    store = _sqlite_store(tmp_path, monkeypatch)
    runner = _runner(store, _chain())
    source = _source()
    entry = store.get_or_create_session(source)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_fallback_entry_agent_kwargs",
        lambda candidate: (candidate["model"], {"provider": candidate["provider"]}),
    )

    def _fail_write(*args, **kwargs):
        raise RuntimeError("injected routing write failure")

    monkeypatch.setattr(store._db, "replace_gateway_routing_entries", _fail_write)
    response = await runner._handle_fallback_command(
        _event("/fallback on", source=source)
    )

    assert "could not be persisted" in response
    assert store.get_session_metadata(entry.session_key, _MANUAL_KEY) is None
    runner._evict_cached_agent.assert_not_called()
    reloaded = SessionStore(store.sessions_dir, GatewayConfig())
    assert reloaded.get_session_metadata(entry.session_key, _MANUAL_KEY) is None


@pytest.mark.asyncio
async def test_fallback_off_rolls_back_when_primary_routing_write_fails(
    tmp_path, monkeypatch
):
    store = _sqlite_store(tmp_path, monkeypatch)
    runner = _runner(store, _chain())
    source = _source()
    entry = store.get_or_create_session(source)
    assert store.set_session_metadata(entry.session_key, _MANUAL_KEY, 0)

    def _fail_write(*args, **kwargs):
        raise RuntimeError("injected routing write failure")

    monkeypatch.setattr(store._db, "replace_gateway_routing_entries", _fail_write)
    response = await runner._handle_fallback_command(
        _event("/fallback off", source=source)
    )

    assert "could not be persisted" in response
    assert store.get_session_metadata(entry.session_key, _MANUAL_KEY) == 0
    runner._evict_cached_agent.assert_not_called()
    reloaded = SessionStore(store.sessions_dir, GatewayConfig())
    assert reloaded.get_session_metadata(entry.session_key, _MANUAL_KEY) == 0


@pytest.mark.asyncio
async def test_fallback_rechecks_busy_state_after_source_normalization(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, _chain())
    raw_source = _source(thread_id="general")
    target_source = _source(thread_id="recovered-topic")
    target_entry = store.get_or_create_session(target_source)
    runner._normalize_source_for_session_key = lambda source: target_source
    runner._running_agents[target_entry.session_key] = object()
    monkeypatch.setattr(
        gateway_run,
        "_resolve_fallback_entry_agent_kwargs",
        lambda candidate: (candidate["model"], {"provider": candidate["provider"]}),
    )

    response = await runner._handle_fallback_command(
        _event("/fallback on", source=raw_source)
    )

    assert "can't run mid-turn" in response
    assert store.get_session_metadata(target_entry.session_key, _MANUAL_KEY) is None
    runner._evict_cached_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "mutation_name"),
    (("on", "set_session_metadata"), ("off", "delete_session_metadata")),
)
async def test_fallback_mutation_reserves_session_until_persistence_finishes(
    tmp_path, monkeypatch, action, mutation_name
):
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, _chain())
    source = _source()
    entry = store.get_or_create_session(source)
    if action == "off":
        assert store.set_session_metadata(entry.session_key, _MANUAL_KEY, 0)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_fallback_entry_agent_kwargs",
        lambda candidate: (candidate["model"], {"provider": candidate["provider"]}),
    )
    competing_turn = object()
    competing_turn_started = False
    running_slot_seen_during_persistence = False
    base_facade = runner.async_session_store

    class _RacingFacade:
        def __getattr__(self, name):
            return getattr(base_facade, name)

        async def set_session_metadata(self, *args):
            await self._race("set_session_metadata")
            return await base_facade.set_session_metadata(*args)

        async def delete_session_metadata(self, *args):
            await self._race("delete_session_metadata")
            return await base_facade.delete_session_metadata(*args)

        async def _race(self, current_mutation):
            nonlocal competing_turn_started, running_slot_seen_during_persistence
            if current_mutation != mutation_name:
                return
            running_slot_seen_during_persistence = (
                entry.session_key in runner._running_agents
            )
            route_mutation_active = bool(
                getattr(runner, "_is_routing_mutation_active", lambda _key: False)(
                    entry.session_key
                )
            )
            if not running_slot_seen_during_persistence and not route_mutation_active:
                runner._running_agents[entry.session_key] = competing_turn
                competing_turn_started = True

    setattr(runner, "_async_session_store", _RacingFacade())

    response = await runner._handle_fallback_command(
        _event(f"/fallback {action}", source=source)
    )

    assert competing_turn_started is False
    assert running_slot_seen_during_persistence is False
    assert entry.session_key not in runner._running_agents
    assert not getattr(runner, "_is_routing_mutation_active", lambda _key: False)(
        entry.session_key
    )
    assert f"Manual fallback: {action.upper()}" in response


def test_routing_mutation_blocks_canonical_turn_claim(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, _chain())
    source = _source()
    entry = store.get_or_create_session(source)
    runner.__dict__["_routing_mutations"] = {entry.session_key}
    acquire = MagicMock(return_value=(object(), None))
    monkeypatch.setattr(
        "hermes_cli.active_sessions.try_acquire_active_session",
        acquire,
    )

    lease, message = runner._claim_active_session_slot(entry.session_key, source)

    assert lease is None
    assert message is not None and "routing" in message.lower()
    acquire.assert_not_called()


def test_routing_mutation_has_explicit_event_rejection(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, _chain())
    source = _source()
    entry = store.get_or_create_session(source)
    runner.__dict__["_routing_mutations"] = {entry.session_key}

    message = runner._routing_mutation_rejection(entry.session_key)

    assert message is not None
    assert "routing" in message.lower()
    assert "resend" in message.lower()


@pytest.mark.asyncio
async def test_fallback_route_labels_are_force_redacted(tmp_path, monkeypatch):
    secret_like_model = "token=fixture-only-redaction-value"
    chain = _chain()
    chain[0]["model"] = secret_like_model
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, chain)
    source = _source()
    monkeypatch.setattr(
        gateway_run,
        "_resolve_fallback_entry_agent_kwargs",
        lambda candidate: (
            candidate["model"],
            {
                "provider": candidate["provider"],
                "api_key": "usable-fixture-credential",
                "base_url": candidate["base_url"],
                "api_mode": "chat_completions",
            },
        ),
    )

    enabled = await runner._handle_fallback_command(
        _event("/fallback on", source=source)
    )
    status = await runner._handle_fallback_command(
        _event("/fallback status", source=source)
    )

    assert secret_like_model not in enabled
    assert secret_like_model not in status


@pytest.mark.asyncio
async def test_fallback_command_uses_secondary_ingress_profile_scope(
    tmp_path, monkeypatch
):
    secondary_home = tmp_path / "secondary"
    secondary_home.mkdir()
    (secondary_home / "config.yaml").write_text(
        "fallback_providers:\n"
        "  - provider: openrouter\n"
        "    model: openai/secondary-route\n"
        "    base_url: https://openrouter.ai/api/v1\n"
        "    api_key: usable-secondary-fixture-key\n",
        encoding="utf-8",
    )
    store = _store(tmp_path, monkeypatch)
    runner = _runner(store, [])
    del runner._refresh_fallback_model
    runner._fallback_model = None
    runner._configured_profile_name = lambda: "default"
    runner._resolve_profile_home_for_source = lambda source: secondary_home

    async def _dispatch(event):
        return await runner._handle_fallback_command(event)

    runner._handle_message = _dispatch
    monkeypatch.setattr(
        gateway_run,
        "_resolve_fallback_entry_agent_kwargs",
        lambda candidate: (candidate["model"], {"provider": candidate["provider"]}),
    )
    source = _source(profile="secondary")

    response = await runner._make_default_profile_message_handler()(
        _event("/fallback on", source=source)
    )

    assert response is not None
    assert "openai/secondary-route" in response


def test_fallback_refresh_reads_context_scoped_profile_home(tmp_path, monkeypatch):
    default_home = tmp_path / "default"
    secondary_home = tmp_path / "secondary"
    default_home.mkdir()
    secondary_home.mkdir()
    (default_home / "config.yaml").write_text(
        "fallback_providers:\n"
        "  - provider: default-provider\n"
        "    model: default-model\n"
        "    api_key: default-inline-key\n",
        encoding="utf-8",
    )
    (secondary_home / "config.yaml").write_text(
        "fallback_providers:\n"
        "  - provider: secondary-provider\n"
        "    model: secondary-model\n"
        "    api_key: secondary-inline-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", default_home)
    runner = object.__new__(GatewayRunner)
    runner._fallback_model = None

    token = set_hermes_home_override(secondary_home)
    try:
        chain = runner._refresh_fallback_model()
    finally:
        reset_hermes_home_override(token)

    assert chain is not None
    assert chain[0]["provider"] == "secondary-provider"
    assert chain[0]["model"] == "secondary-model"
    assert chain[0]["api_key"] == "secondary-inline-key"


def test_fallback_refresh_expands_env_refs_from_active_profile_secret_scope(
    tmp_path, monkeypatch
):
    from agent.secret_scope import (
        is_multiplex_active,
        reset_secret_scope,
        set_multiplex_active,
        set_secret_scope,
    )

    home = tmp_path / "secondary"
    home.mkdir()
    (home / "config.yaml").write_text(
        "fallback_providers:\n"
        "  - provider: secondary-provider\n"
        "    model: secondary-model\n"
        "    base_url: https://secondary.example/v1\n"
        "    api_key: ${PROFILE_FALLBACK_KEY}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path / "default")
    monkeypatch.setenv("PROFILE_FALLBACK_KEY", "wrong-default-profile-key")
    runner = object.__new__(GatewayRunner)
    runner._fallback_model = None
    previous_multiplex = is_multiplex_active()
    set_multiplex_active(True)
    secret_token = set_secret_scope(
        {"PROFILE_FALLBACK_KEY": "correct-secondary-profile-key"}
    )
    home_token = set_hermes_home_override(home)
    try:
        chain = runner._refresh_fallback_model()
    finally:
        reset_hermes_home_override(home_token)
        reset_secret_scope(secret_token)
        set_multiplex_active(previous_multiplex)

    assert chain is not None
    assert chain[0]["api_key"] == "correct-secondary-profile-key"


def test_fallback_refresh_serializes_last_known_good_cache_publication(
    tmp_path, monkeypatch
):
    home = tmp_path / "default"
    home.mkdir()
    (home / "config.yaml").write_text("fallback_providers: []\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    runner = object.__new__(GatewayRunner)
    runner._fallback_model = None
    runner._fallback_models_by_home = {str(home.resolve()): None}
    runner._fallback_model_refresh_lock = threading.RLock()
    first_entered = threading.Event()
    second_returned = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    def _config(provider):
        return {
            "fallback_providers": [
                {
                    "provider": provider,
                    "model": f"{provider}-model",
                    "api_key": f"{provider}-fixture-key",
                }
            ]
        }

    def _read_user_config_raw(_path):
        nonlocal call_count
        with call_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_entered.set()
            second_returned.wait(timeout=0.1)
            return _config("older")
        second_returned.set()
        return _config("newer")

    monkeypatch.setattr(
        "hermes_cli.config.read_user_config_raw", _read_user_config_raw
    )
    first = threading.Thread(target=runner._refresh_fallback_model)
    second = threading.Thread(target=runner._refresh_fallback_model)
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert call_count == 2
    assert runner._fallback_models_by_home[str(home.resolve())][0]["provider"] == "newer"


def test_fallback_refresh_failure_never_reuses_another_profile_cache(
    tmp_path, monkeypatch
):
    default_home = tmp_path / "default"
    secondary_home = tmp_path / "secondary"
    default_home.mkdir()
    secondary_home.mkdir()
    (default_home / "config.yaml").write_text(
        "fallback_providers: [",
        encoding="utf-8",
    )
    (secondary_home / "config.yaml").write_text(
        "fallback_providers:\n"
        "  - provider: secondary-provider\n"
        "    model: secondary-model\n"
        "    api_key: secondary-inline-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", default_home)
    runner = object.__new__(GatewayRunner)
    runner._fallback_model = None

    token = set_hermes_home_override(secondary_home)
    try:
        secondary_chain = runner._refresh_fallback_model()
    finally:
        reset_hermes_home_override(token)

    token = set_hermes_home_override(default_home)
    try:
        default_chain = runner._refresh_fallback_model()
    finally:
        reset_hermes_home_override(token)

    assert secondary_chain is not None
    assert secondary_chain[0]["provider"] == "secondary-provider"
    assert default_chain is None


def test_multiplex_gateway_runner_startup_scopes_profile_provider_secrets(
    tmp_path, monkeypatch
):
    from agent.secret_scope import is_multiplex_active, set_multiplex_active

    home = tmp_path / "default-profile"
    home.mkdir()
    (home / ".env").write_text(
        "PROFILE_KEY=profile-scoped-fixture-key\n", encoding="utf-8"
    )
    (home / "config.yaml").write_text(
        "providers:\n"
        "  profile-provider:\n"
        "    name: profile-provider\n"
        "    base_url: https://profile.example/v1\n"
        "    api_key: ${PROFILE_KEY}\n"
        "fallback_providers:\n"
        "  - provider: profile-provider\n"
        "    model: profile-model\n"
        "    base_url: https://profile.example/v1\n"
        "    api_key: ${PROFILE_KEY}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    previous_multiplex = is_multiplex_active()
    try:
        runner = GatewayRunner(
            GatewayConfig(
                multiplex_profiles=True,
                sessions_dir=home / "sessions",
            )
        )
    finally:
        set_multiplex_active(previous_multiplex)

    assert runner._fallback_model is not None
    assert runner._fallback_model[0]["provider"] == "profile-provider"
    assert runner._fallback_model[0]["api_key"] == "profile-scoped-fixture-key"
