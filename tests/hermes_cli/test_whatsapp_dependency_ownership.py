import subprocess
from pathlib import Path

import pytest

from hermes_cli.main_platform_setup import _whatsapp_install_bridge
from hermes_cli.web_routers.messaging import _ensure_whatsapp_bridge_dependencies


@pytest.fixture(autouse=True)
def _no_real_subprocesses(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("unexpected Popen"))


def test_explicit_maintenance_paths_refresh_and_stamp_whatsapp_dependencies(
    tmp_path, monkeypatch
):
    import hermes_cli.main as hm
    import hermes_constants

    checkout = tmp_path / "checkout"
    bridge_dir = checkout / "scripts" / "whatsapp-bridge"
    checkout.mkdir()
    (checkout / "package.json").write_text("{}", encoding="utf-8")
    (bridge_dir / "node_modules").mkdir(parents=True)
    (bridge_dir / "bridge.js").write_text("// bridge")
    (bridge_dir / "package.json").write_text(
        '{"dependencies": {}}', encoding="utf-8"
    )
    (bridge_dir / "package-lock.json").write_text(
        '{"lockfileVersion": 3}', encoding="utf-8"
    )

    monkeypatch.setattr(hm, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(
        hermes_constants,
        "find_node_executable",
        lambda _name: "/usr/bin/npm",
    )
    monkeypatch.setattr(
        hermes_constants,
        "with_hermes_node_path",
        lambda _env=None: {},
    )

    installs = []
    phase = ["cli"]

    def fake_run(command, *, cwd, **kwargs):
        installs.append(phase[0])
        assert command[1] == "ci"
        assert Path(cwd) != bridge_dir
        (Path(cwd) / "node_modules").mkdir()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert _whatsapp_install_bridge(bridge_dir) == bridge_dir
    stamp = bridge_dir / "node_modules" / ".hermes-pkg-hash"
    cli_stamp = stamp.read_text(encoding="utf-8").strip()
    assert cli_stamp

    phase[0] = "dashboard"
    (bridge_dir / "package.json").write_text(
        '{"dependencies": {"a": "1"}}', encoding="utf-8"
    )
    _ensure_whatsapp_bridge_dependencies(bridge_dir)
    dashboard_stamp = stamp.read_text(encoding="utf-8").strip()
    assert dashboard_stamp and dashboard_stamp != cli_stamp

    phase[0] = "update"
    (bridge_dir / "package-lock.json").write_text(
        '{"lockfileVersion": 3, "packages": {"a": {}}}', encoding="utf-8"
    )

    def fake_deterministic_install(*_args, **_kwargs):
        installs.append(phase[0])
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(hm, "_run_npm_install_deterministic", fake_deterministic_install)
    monkeypatch.setattr(hm, "_resolve_node_runtime_npm", lambda: "/usr/bin/npm")
    monkeypatch.setattr(hm, "_nixos_build_env", lambda: {})
    monkeypatch.setattr(hm, "_npm_lockfile_changed", lambda _root: False)
    monkeypatch.setattr(
        hermes_constants,
        "get_default_hermes_root",
        lambda: tmp_path / "hermes-home",
    )
    monkeypatch.setattr(
        "tools.browser_tool_install.warm_agent_browser_npx_cache",
        lambda: True,
    )
    from hermes_cli.update_cmd_deps import _update_node_dependencies

    assert _update_node_dependencies() == []
    update_stamp = stamp.read_text(encoding="utf-8").strip()
    assert update_stamp and update_stamp != dashboard_stamp
    assert installs == ["cli", "dashboard", "update"]

@pytest.mark.parametrize("relocated", [False, True])
def test_explicit_callers_share_the_owner_and_translate_success(tmp_path, monkeypatch, relocated):
    from gateway.platforms import whatsapp_common
    import hermes_cli.main as hm
    from hermes_cli.update_cmd_deps import _update_whatsapp_bridge_dependencies

    bridge = tmp_path / "scripts" / "whatsapp-bridge"
    (bridge / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
    calls = []
    env = {"PYTHON": "/nix/store/python"}

    def shared_owner(target, **kwargs):
        calls.append((target, kwargs))
        return tmp_path / "prepared" if relocated else target

    monkeypatch.setattr(whatsapp_common, "prepare_whatsapp_bridge_runtime", shared_owner)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("caller installed independently"))
    expected = tmp_path / "prepared" if relocated else bridge
    assert _whatsapp_install_bridge(bridge) == expected
    assert _ensure_whatsapp_bridge_dependencies(bridge) == expected
    assert _update_whatsapp_bridge_dependencies("/resolved/npm", env) is True
    assert calls == [(bridge, {}), (bridge, {}), (bridge, {"npm": "/resolved/npm", "env": env})]


def test_explicit_callers_translate_typed_failure_and_updater_label(tmp_path, monkeypatch, capsys):
    from fastapi import HTTPException
    from gateway.platforms import whatsapp_common
    import hermes_cli.main as hm
    from hermes_cli.update_cmd_deps import _update_node_dependencies

    bridge = tmp_path / "scripts" / "whatsapp-bridge"
    (bridge / "node_modules").mkdir(parents=True)
    (tmp_path / "package.json").write_text("{}")
    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hm, "_resolve_node_runtime_npm", lambda: "/resolved/npm")
    monkeypatch.setattr(hm, "_nixos_build_env", lambda: {})
    monkeypatch.setattr(hm, "_npm_lockfile_changed", lambda _: False)
    monkeypatch.setattr("tools.browser_tool_install.warm_agent_browser_npx_cache", lambda: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("independent install"))

    def failure(*args, **kwargs):
        raise whatsapp_common.WhatsAppBridgeDependencyError("transaction failed")

    monkeypatch.setattr(whatsapp_common, "prepare_whatsapp_bridge_runtime", failure)
    assert _whatsapp_install_bridge(bridge) is None
    with pytest.raises(HTTPException) as error:
        _ensure_whatsapp_bridge_dependencies(bridge)
    assert error.value.status_code == 500
    assert error.value.detail == "transaction failed"
    assert _update_node_dependencies() == ["WhatsApp bridge"]
    assert "transaction failed" in capsys.readouterr().out


def test_updater_preserves_resolved_npm_and_nix_python_in_filtered_environment(tmp_path, monkeypatch):
    import hermes_cli.main as hm
    import hermes_constants
    from hermes_cli.update_cmd_deps import _update_node_dependencies

    bridge = tmp_path / "scripts" / "whatsapp-bridge"
    (bridge / "node_modules").mkdir(parents=True)
    (tmp_path / "package.json").write_text("{}")
    (bridge / "package.json").write_text("{}")
    (bridge / "package-lock.json").write_text('{"lockfileVersion": 3}')
    (bridge / "bridge.js").write_text("// bridge")
    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hm, "_resolve_node_runtime_npm", lambda: "/resolved/npm")
    monkeypatch.setattr(hm, "_run_npm_install_deterministic",
                        lambda *a, **kw: pytest.fail("legacy in-place fallback helper used"))
    build_env = {"PYTHON": "/nix/store/python/bin/python3", "PATH": "/safe/bin",
                 "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path),
                 "NPM_CONFIG_CACHE": str(tmp_path / "cache"),
                 "OPENAI_API_KEY": "provider-secret", "NPM_TOKEN": "registry-secret",
                 "WHATSAPP_ACCESS_TOKEN": "messaging-secret"}
    monkeypatch.setattr(hm, "_nixos_build_env", lambda: dict(build_env))
    monkeypatch.setattr(hm, "_npm_lockfile_changed", lambda _: False)
    monkeypatch.setattr("tools.browser_tool_install.warm_agent_browser_npx_cache", lambda: True)
    monkeypatch.setattr(hermes_constants, "find_node_executable", lambda _: pytest.fail("resolved npm discarded"))

    def managed_path(env=None):
        result = dict(env or {})
        if not result.get("PATH", "").startswith("/managed/node:"):
            result["PATH"] = "/managed/node:" + result.get("PATH", "")
        return result

    monkeypatch.setattr(hermes_constants, "with_hermes_node_path", managed_path)
    calls = []

    def fake_npm(command, *, cwd, env, **kwargs):
        calls.append(command)
        assert command == ["/resolved/npm", "ci", "--silent"]
        assert Path(cwd) != bridge
        assert env["PYTHON"] == build_env["PYTHON"]
        assert env["PATH"] == "/managed/node:/safe/bin"
        for key in ("HOME", "TMPDIR", "NPM_CONFIG_CACHE"):
            assert env[key] == build_env[key]
        assert not {"OPENAI_API_KEY", "NPM_TOKEN", "WHATSAPP_ACCESS_TOKEN"} & env.keys()
        (Path(cwd) / "node_modules").mkdir()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_npm)
    assert _update_node_dependencies() == []
    assert len(calls) == 1
