"""Shared WhatsApp bridge resolution and caller integration tests.

Regression coverage for #49561: read-only installs must use one transactional
resolver in ``whatsapp_common`` so the adapter, dashboard pairing, CLI
onboarding, and doctor cannot disagree about the active runtime directory.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from subprocess import CompletedProcess

from gateway.platforms import whatsapp_common


_RUNTIME_SOURCE_FILES = (
    "bridge.js",
    "bridge_helpers.js",
    "allowlist.js",
    "outbound_ids.js",
    "owner_message_gate.js",
    "package.json",
    "package-lock.json",
)


def _seed_runtime(bridge_dir: Path, version: str) -> None:
    bridge_dir.mkdir(parents=True, exist_ok=True)
    for name in _RUNTIME_SOURCE_FILES:
        if name == "package.json":
            content = json.dumps(
                {
                    "name": "hermes-whatsapp-bridge",
                    "version": version,
                    "hermesRuntimeFiles": list(_RUNTIME_SOURCE_FILES),
                    "dependencies": {"example": version},
                },
                sort_keys=True,
            )
        elif name == "package-lock.json":
            content = json.dumps(
                {
                    "name": "hermes-whatsapp-bridge",
                    "version": version,
                    "lockfileVersion": 3,
                    "packages": {"": {"version": version}},
                },
                sort_keys=True,
            )
        else:
            content = f"// {name} v{version}\n"
        (bridge_dir / name).write_text(content, encoding="utf-8")


def _successful_npm_ci(calls: list[Path]):
    def run(command, *, cwd, **kwargs):
        stage = Path(cwd)
        calls.append(stage)
        assert command[-2:] == ["ci", "--silent"]
        (stage / "node_modules").mkdir()
        (stage / "node_modules" / "installed").write_text(
            "fresh\n", encoding="utf-8"
        )
        return CompletedProcess(command, 0, stdout="", stderr="")

    return run


def test_writable_install_returns_install_dir_without_npm(tmp_path, monkeypatch):
    install_bridge = tmp_path / "install" / "scripts" / "whatsapp-bridge"
    persistent = tmp_path / "home" / "scripts" / "whatsapp-bridge"
    _seed_runtime(install_bridge, "2.0.0")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("writable source installs must not invoke npm in the resolver")

    monkeypatch.setattr(whatsapp_common.subprocess, "run", unexpected_run)

    resolved = whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=install_bridge,
        persistent_bridge=persistent,
        install_writable=True,
    )

    assert resolved == install_bridge
    assert not persistent.exists()


def test_default_readonly_install_builds_transactional_hermes_home_mirror(
    tmp_path, monkeypatch
):

    install_root = tmp_path / "install"
    install_bridge = install_root / "scripts" / "whatsapp-bridge"
    hermes_home = tmp_path / "home"
    persistent = hermes_home / "scripts" / "whatsapp-bridge"
    _seed_runtime(install_bridge, "2.0.0")
    calls: list[Path] = []
    monkeypatch.setattr(
        whatsapp_common,
        "__file__",
        str(install_root / "gateway" / "platforms" / "whatsapp_common.py"),
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(whatsapp_common, "_bridge_dir_is_writable", lambda path: False)
    monkeypatch.setattr(whatsapp_common, "find_node_executable", lambda name: "npm")
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )

    # This is the zero-argument path used unchanged by web_server, main, doctor,
    # and the plugin adapter.
    resolved = whatsapp_common.resolve_whatsapp_bridge_dir()

    assert resolved == persistent
    assert len(calls) == 1
    assert calls[0] != persistent
    assert (persistent / "bridge.js").read_bytes() == (
        install_bridge / "bridge.js"
    ).read_bytes()
    assert (persistent / "node_modules" / "installed").exists()


def test_readonly_install_refreshes_stale_mirror_and_preserves_user_state(
    tmp_path, monkeypatch
):
    install_bridge = tmp_path / "install" / "scripts" / "whatsapp-bridge"
    persistent = tmp_path / "home" / "scripts" / "whatsapp-bridge"
    _seed_runtime(install_bridge, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    (persistent / "node_modules" / "old").write_text("stale\n", encoding="utf-8")
    (persistent / "auth" / "session").mkdir(parents=True)
    (persistent / "auth" / "session" / "creds.json").write_text(
        "secret\n", encoding="utf-8"
    )
    calls: list[Path] = []
    monkeypatch.setattr(whatsapp_common, "find_node_executable", lambda name: "npm")
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )

    resolved = whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=install_bridge,
        persistent_bridge=persistent,
        install_writable=False,
    )

    assert resolved == persistent
    assert len(calls) == 1
    assert (persistent / "bridge.js").read_text(encoding="utf-8").endswith(
        "v2.0.0\n"
    )
    assert not (persistent / "node_modules" / "old").exists()
    assert (persistent / "node_modules" / "installed").exists()
    assert (persistent / "auth" / "session" / "creds.json").read_text(
        encoding="utf-8"
    ) == "secret\n"


def test_runtime_inventory_covers_all_top_level_bridge_imports_and_lists_tests_separately():
    repo_root = Path(__file__).resolve().parents[2]
    bridge_root = repo_root / "scripts" / "whatsapp-bridge"
    bridge_source = (bridge_root / "bridge.js").read_text(encoding="utf-8")
    imported_runtime_files = set(
        re.findall(r"from\s+['\"]\./([^'\"]+)['\"]", bridge_source)
    )
    expected_runtime_files = {
        "bridge.js",
        "package.json",
        "package-lock.json",
        *imported_runtime_files,
    }
    project_tests = {path.name for path in bridge_root.glob("*.test.mjs")}
    package_inventory = tuple(
        json.loads((bridge_root / "package.json").read_text(encoding="utf-8"))[
            "hermesRuntimeFiles"
        ]
    )

    assert package_inventory == whatsapp_common.WHATSAPP_BRIDGE_RUNTIME_FILES
    assert set(package_inventory) == expected_runtime_files
    assert set(whatsapp_common.WHATSAPP_BRIDGE_PROJECT_TEST_FILES) == project_tests
    assert expected_runtime_files.isdisjoint(project_tests)


def test_web_cli_and_doctor_call_the_shared_public_resolver():
    """All pre-existing callers must inherit common's transactional semantics."""
    repo_root = Path(__file__).resolve().parents[2]
    caller_files = (
        repo_root / "hermes_cli" / "web_server.py",
        repo_root / "hermes_cli" / "main.py",
        repo_root / "hermes_cli" / "doctor.py",
    )

    for caller_file in caller_files:
        tree = ast.parse(caller_file.read_text(encoding="utf-8"))
        shared_imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "gateway.platforms.whatsapp_common"
            and any(
                alias.name == "resolve_whatsapp_bridge_dir" for alias in node.names
            )
        ]
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "resolve_whatsapp_bridge_dir"
        ]
        assert shared_imports, f"{caller_file} does not import the shared resolver"
        assert calls, f"{caller_file} does not call the shared resolver"

    for caller_file in caller_files[:2]:
        tree = ast.parse(caller_file.read_text(encoding="utf-8"))
        shared_installer_imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "gateway.platforms.whatsapp_common"
            and any(
                alias.name == "ensure_whatsapp_bridge_dependencies"
                for alias in node.names
            )
        ]
        installer_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ensure_whatsapp_bridge_dependencies"
        ]
        assert shared_installer_imports, (
            f"{caller_file} does not import the shared dependency installer"
        )
        assert installer_calls, (
            f"{caller_file} does not call the shared dependency installer"
        )


def test_supported_bridge_launches_keep_mutable_session_state_outside_runtime():
    """Prove updates need no destructive live-bridge kill or backup writer merge."""
    repo_root = Path(__file__).resolve().parents[2]
    launch_contracts = {
        repo_root / "plugins" / "platforms" / "whatsapp" / "adapter.py": (
            '"--session", str(self._session_path)',
        ),
        repo_root / "hermes_cli" / "main.py": (
            '"--session",',
            "str(session_dir)",
        ),
        repo_root / "hermes_cli" / "web_server.py": (
            '"--session",',
            "str(session_path)",
        ),
    }
    for caller, required_fragments in launch_contracts.items():
        source = caller.read_text(encoding="utf-8")
        for fragment in required_fragments:
            assert fragment in source, f"{caller} lost explicit external session routing"

    common_source = (
        repo_root / "gateway" / "platforms" / "whatsapp_common.py"
    ).read_text(encoding="utf-8")
    assert "_kill_stale_bridge_by_pidfile" not in common_source
    assert "_kill_port_process" not in common_source

