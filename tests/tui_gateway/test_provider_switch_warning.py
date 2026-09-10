"""Native warning propagation preserves routing, confirmation and session ownership."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from hermes_cli import model_switch as ms


@pytest.fixture
def offline_switch(monkeypatch):
    """Exercise the real pipeline; replace catalogs, metadata and external I/O."""
    config = {"model": {"default": "old-model", "provider": "openai-codex"}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    save = Mock(side_effect=AssertionError("switch resolution must not persist config"))
    monkeypatch.setattr("hermes_cli.config.save_config", save)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr("agent.usage_pricing.get_pricing_entry", lambda *a, **k: None)
    monkeypatch.setattr(ms, "list_provider_models", lambda *a, **k: [])
    monkeypatch.setattr(ms, "DIRECT_ALIASES", {})
    monkeypatch.setattr(ms, "_DIRECT_ALIAS_LOADED", None)
    catalog = Mock(return_value=[])
    monkeypatch.setattr("hermes_cli.models.cached_provider_model_ids", catalog)
    monkeypatch.setattr("hermes_cli.models.provider_model_ids", catalog)
    monkeypatch.setattr("hermes_cli.models.model_ids", lambda: ["vendor/foreign-model"])
    runtime = Mock(return_value={
        "api_key": "test-key", "base_url": "https://example.invalid/v1",
        "api_mode": "chat_completions",
    })
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", runtime)
    monkeypatch.setattr("hermes_cli.runtime_provider._get_named_custom_provider", lambda p: None)
    validation = {"accepted": True, "recognized": True, "message": "Catalog validation notice."}
    monkeypatch.setattr("hermes_cli.models.validate_requested_model", lambda *a, **k: validation)
    capabilities = SimpleNamespace(vision=True)
    model_info = SimpleNamespace(context_window=12345)
    monkeypatch.setattr(ms, "get_model_capabilities", lambda *a, **k: capabilities)
    monkeypatch.setattr(ms, "get_model_info", lambda *a, **k: model_info)
    monkeypatch.setattr("agent.native_compaction.resolve_native_compaction_capabilities", lambda **k: {"native_compaction": True})
    return SimpleNamespace(
        config=config, catalog=catalog, runtime=runtime, validation=validation,
        capabilities=capabilities, model_info=model_info, save=save,
    )


def _switch(**kwargs):
    args: dict = dict(raw_input="foreign-model", current_provider="openai-codex", current_model="old-model")
    args.update(kwargs)
    return ms.switch_model(**args)


def _assert_warning(result, previous, target):
    assert result.success, result.error_message
    warning = getattr(result, "provider_switch_warning", "")
    assert f"{previous} -> {target}" in warning
    assert "No --provider argument was supplied" in warning
    assert "additional costs" in warning
    assert "Specify --provider explicitly" in warning
    assert warning not in result.warning_message

@pytest.fixture
def tui_switch(offline_switch, monkeypatch, tmp_path):
    """Keep resolution, confirmation, live application and config.set dispatch real."""
    test_home = tmp_path / "tui-home"
    test_hermes_home = test_home / ".hermes"
    test_hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(test_home))
    monkeypatch.setenv("HERMES_HOME", str(test_hermes_home))
    from tui_gateway import server

    # Known synthetic prices exercise the real cost guard without a pricing lookup.
    offline_switch.model_info.has_cost_data = lambda: True
    offline_switch.model_info.cost_input = 1
    offline_switch.model_info.cost_output = 1

    class FakeLiveAgent:
        model = "old-model"
        provider = "openai-codex"
        api_key = ""
        base_url = ""
        api_mode = "codex_responses"
        compression_enabled = False
        fail_swap = False

        def switch_model(self, *, new_model, new_provider, api_key, base_url, api_mode, capabilities):
            if self.fail_swap:
                raise RuntimeError("offline swap failure")
            self.model, self.provider = new_model, new_provider
            self.api_key, self.base_url, self.api_mode = api_key, base_url, api_mode

    agent = FakeLiveAgent()
    session = {
        "agent": agent, "session_key": "provider-warning-key", "history": [],
        "history_lock": threading.Lock(), "history_version": 0, "running": False,
        "attached_images": [], "image_counter": 0, "cols": 80, "slash_worker": None,
        "show_reasoning": False, "tool_progress_mode": "all",
    }
    sid = "provider-warning-sid"
    monkeypatch.setattr(server, "_sessions", {sid: session})
    effects = {}
    for name in (
        "_restart_slash_worker", "_persist_live_session_runtime",
        "_persist_live_session_system_prompt", "_append_model_switch_marker",
        "_persist_model_switch",
    ):
        effects[name] = Mock(return_value=None)
        monkeypatch.setattr(server, name, effects[name])

    monkeypatch.setattr(server, "_session_info", Mock(return_value={}))
    monkeypatch.setattr(server, "write_json", Mock())

    def request_model(value, *, confirmed=False):
        return server.handle_request({
            "id": "provider-warning-request", "method": "config.set",
            "params": {"session_id": sid, "key": "model", "value": value,
                       "confirm_expensive_model": confirmed},
        })

    try:
        yield SimpleNamespace(agent=agent, session=session, request=request_model, effects=effects)
    finally:
        server._sessions.pop(sid, None)


@pytest.mark.parametrize("case", ["immediate", "confirmed-idle", "explicit-provider", "same-provider"])
def test_tui_applied_response_preserves_resolver_warnings(offline_switch, tui_switch, case):
    raw = "foreign-model --session"
    expected_model, expected_provider = "vendor/foreign-model", "openrouter"
    if case == "explicit-provider":
        raw = "vendor/foreign-model --provider openrouter --session"
    elif case == "same-provider":
        offline_switch.catalog.return_value = ["foreign-model"]
        expected_model, expected_provider = "foreign-model", "openai-codex"
    elif case == "confirmed-idle":
        offline_switch.model_info.cost_input = 30
        pending = tui_switch.request(raw)
        assert "error" not in pending, pending
        pending_result = pending["result"]
        assert pending_result["confirm_required"] is True
        assert pending_result["warning"] == pending_result["confirm_message"]
        assert "EXPENSIVE MODEL WARNING" in pending_result["confirm_message"]
        assert pending_result["warning"].endswith("\n\nCatalog validation notice.")
        assert "PROVIDER AUTOMATICALLY CHANGED" not in str(pending)
        assert (tui_switch.agent.model, tui_switch.agent.provider) == ("old-model", "openai-codex")
        assert "model_override" not in tui_switch.session
        for effect in tui_switch.effects.values():
            effect.assert_not_called()

    response = tui_switch.request(raw, confirmed=case == "confirmed-idle")

    assert "error" not in response, response
    result = response["result"]
    assert result["confirm_required"] is False
    assert result["confirm_message"] == ""
    assert result["scope"] == "session"
    assert result["value"] == tui_switch.agent.model == expected_model
    assert tui_switch.agent.provider == expected_provider
    override = tui_switch.session["model_override"]
    assert override["model"] == expected_model
    assert override["provider"] == expected_provider
    for key in ("api_key", "base_url", "api_mode"):
        assert override[key] == getattr(tui_switch.agent, key)
    tui_switch.effects["_persist_model_switch"].assert_not_called()
    assert "provider_switch_warning" not in result
    if case in ("immediate", "confirmed-idle"):
        warning = result["warning"]
        assert warning.startswith("PROVIDER AUTOMATICALLY CHANGED: openai-codex -> openrouter. ")
        assert "No --provider argument was supplied" in warning
        assert "additional costs" in warning
        assert "Specify --provider explicitly" in warning
        assert warning.endswith("\n\nCatalog validation notice.")
    else:
        assert result["warning"] == "Catalog validation notice."


@pytest.mark.parametrize("case", ["implicit", "confirmed", "explicit-provider", "same-provider", "empty-warning"])
def test_tui_deferred_application_emits_resolver_warning_once(offline_switch, tui_switch, monkeypatch, case):
    from tui_gateway import server

    sid = "provider-warning-sid"
    raw = "foreign-model --session"
    model, provider = "vendor/foreign-model", "openrouter"
    explicit_provider = ""
    if case in ("explicit-provider", "empty-warning"):
        raw = "vendor/foreign-model --provider openrouter --session"
        explicit_provider = "openrouter"
        if case == "empty-warning":
            offline_switch.validation["message"] = ""
    elif case == "same-provider":
        offline_switch.catalog.return_value = ["foreign-model"]
        model, provider = "foreign-model", "openai-codex"
    elif case == "confirmed":
        offline_switch.model_info.cost_input = 30

    resolved = _switch(raw_input=raw.split()[0], explicit_provider=explicit_provider)
    assert resolved.success
    warning = "\n\n".join(w for w in (resolved.provider_switch_warning, resolved.warning_message) if w)
    if case in ("implicit", "confirmed"):
        _assert_warning(resolved, "openai-codex", "openrouter")
        assert warning.endswith("\n\nCatalog validation notice.")
    else:
        assert warning == ("" if case == "empty-warning" else "Catalog validation notice.")

    emit = Mock()
    monkeypatch.setattr(server, "_emit", emit)
    monkeypatch.setattr(server.time, "time", lambda: 1_700_000_000.25)
    tui_switch.session["running"] = True
    response = tui_switch.request(raw, confirmed=case == "confirmed")

    assert "error" not in response, response
    assert response["result"]["deferred"] is True
    assert response["result"]["confirm_required"] is False
    assert response["result"]["warning"] == ""
    assert (tui_switch.agent.model, tui_switch.agent.provider) == ("old-model", "openai-codex")
    assert "model_override" not in tui_switch.session
    assert tui_switch.session["pending_model_switch"]["raw"] == raw
    assert tui_switch.session["pending_model_switch"]["confirm_expensive_model"] is (case == "confirmed")
    emit.assert_not_called()
    for effect in tui_switch.effects.values():
        effect.assert_not_called()

    # The next turn applies the actual queued request, with real resolution and guards.
    tui_switch.session["running"] = False
    server._apply_pending_model_switch(sid, tui_switch.session)

    assert (tui_switch.agent.model, tui_switch.agent.provider) == (model, provider)
    assert tui_switch.session["model_override"] == {
        "model": model, "provider": provider,
        **{key: getattr(tui_switch.agent, key) for key in ("api_key", "base_url", "api_mode")},
    }
    assert "pending_model_switch" not in tui_switch.session
    if warning:
        assert emit.call_args_list == [
            call("session.info", sid, {}),
            call("status.update", sid, {
                "kind": "model-switch-warning", "text": warning,
                "timestamp": 1_700_000_000.25,
            }),
        ]
    else:
        emit.assert_called_once_with("session.info", sid, {})
    before = list(emit.call_args_list)
    server._apply_pending_model_switch(sid, tui_switch.session)
    assert emit.call_args_list == before
    tui_switch.effects["_persist_model_switch"].assert_not_called()
    offline_switch.save.assert_not_called()


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("case", ["declined-confirmation", "resolver-failure", "swap-failure"])
def test_tui_unapplied_switch_has_no_successful_provider_announcement(
    offline_switch, tui_switch, monkeypatch, case, deferred,
):
    from tui_gateway import server

    emit = Mock()
    monkeypatch.setattr(server, "_emit", emit)
    if deferred:
        tui_switch.session["running"] = True
        queued = tui_switch.request("foreign-model --session")
        assert queued["result"]["deferred"] is True
        assert queued["result"]["warning"] == ""
        emit.assert_not_called()
        tui_switch.session["running"] = False

    if case == "declined-confirmation":
        offline_switch.model_info.cost_input = 30
    elif case == "resolver-failure":
        offline_switch.validation.update(accepted=False, message="offline validation failure")
    else:
        tui_switch.agent.fail_swap = True
    runtime_keys = ("model", "provider", "api_key", "base_url", "api_mode")
    before = {key: getattr(tui_switch.agent, key) for key in runtime_keys}

    # Declining means the client never sends the confirmed request.
    if deferred:
        server._apply_pending_model_switch("provider-warning-sid", tui_switch.session)
        assert "pending_model_switch" not in tui_switch.session
        emit.assert_called_once()
        event_type, sid, payload = emit.call_args.args
        assert (event_type, sid) == ("error", "provider-warning-sid")
        response = payload
        before_events = list(emit.call_args_list)
        server._apply_pending_model_switch(sid, tui_switch.session)
        assert emit.call_args_list == before_events
    else:
        response = tui_switch.request("foreign-model --session")
        emit.assert_not_called()

    assert "PROVIDER AUTOMATICALLY CHANGED" not in str(response)
    assert {key: getattr(tui_switch.agent, key) for key in runtime_keys} == before
    assert "model_override" not in tui_switch.session
    for effect in tui_switch.effects.values():
        effect.assert_not_called()
    if deferred:
        if case == "declined-confirmation":
            assert "EXPENSIVE MODEL WARNING" in response["message"]
            assert response["message"].endswith("\n\nCatalog validation notice.")
        else:
            expected_error = "offline validation failure" if case == "resolver-failure" else "offline swap failure"
            assert response["message"].startswith("Could not switch model: ")
            assert expected_error in response["message"]
    elif case == "declined-confirmation":
        assert "error" not in response, response
        result = response["result"]
        assert result["confirm_required"] is True
        assert "EXPENSIVE MODEL WARNING" in result["confirm_message"]
        assert result["warning"] == result["confirm_message"]
        assert result["warning"].endswith("\n\nCatalog validation notice.")
    else:
        assert "result" not in response
        assert response["error"]["code"] == 5001
        expected_error = "offline validation failure" if case == "resolver-failure" else "offline swap failure"
        assert expected_error in response["error"]["message"]


@pytest.mark.parametrize("scope", ["session", "global", "once"])
def test_warning_does_not_change_requested_scope(tui_switch, scope):
    response = tui_switch.request(f"foreign-model --{scope}")

    assert "error" not in response, response
    result = response["result"]
    assert result["scope"] == scope
    assert result["warning"].startswith("PROVIDER AUTOMATICALLY CHANGED:")
    assert result["warning"].endswith("\n\nCatalog validation notice.")
    persist = tui_switch.effects["_persist_model_switch"]
    if scope == "global":
        persist.assert_called_once()
    else:
        persist.assert_not_called()
    if scope == "once":
        assert "model_override" not in tui_switch.session
        restore = tui_switch.session["one_turn_model_restore"]
        assert (restore["model"], restore["provider"]) == ("old-model", "openai-codex")
    else:
        assert "one_turn_model_restore" not in tui_switch.session
        assert tui_switch.session["model_override"]["provider"] == "openrouter"
