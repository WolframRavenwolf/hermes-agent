"""Public launchd lifecycle contracts; never touches a real service or HOME."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import hermes_cli.gateway as gateway_cli
from hermes_cli.service_manager import LaunchdServiceManager


_REAL_REFUSE_TEMP_HOME_SERVICE_WRITE = gateway_cli._refuse_temp_home_service_write


@pytest.fixture(autouse=True)
def _isolated_launchd_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Keep every lifecycle test inside a temporary profile and fake domain."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway_cli, "_launchd_user_home", lambda: home)
    monkeypatch.setattr(gateway_cli, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(
        gateway_cli, "_is_running_inside_gateway_process_tree", lambda: False
    )
    monkeypatch.setattr(
        gateway_cli, "_refuse_temp_home_service_write", lambda *_args: False
    )
    monkeypatch.setattr(gateway_cli, "_clear_launchd_unsupported_marker", lambda: None)
    monkeypatch.setattr(gateway_cli, "read_raw_config", lambda: {})
    return home


def _macos_gateway_cli(monkeypatch: pytest.MonkeyPatch, plist_path: Path) -> None:
    monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
    monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)
    monkeypatch.setattr(gateway_cli, "is_windows", lambda: False)
    monkeypatch.setattr(gateway_cli, "is_container", lambda: False)
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        gateway_cli, "_refuse_gateway_self_management", lambda _action: False
    )
    monkeypatch.setattr(
        gateway_cli, "_dispatch_via_service_manager_if_s6", lambda _action: False
    )
    monkeypatch.setattr(
        gateway_cli,
        "_dispatch_all_via_service_manager_if_s6",
        lambda _action: False,
    )


def _stale_temp_home_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, bytes]:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    old_bytes = b"installed plist bytes\n"
    plist_path.write_bytes(old_bytes)
    temp_plist = plistlib.dumps(
        {"EnvironmentVariables": {"HERMES_HOME": "/tmp/hermes-contract-home"}}
    ).decode("utf-8")

    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        gateway_cli, "_resolve_launchd_app_wrapper_mode", lambda _value=None: False
    )
    monkeypatch.setattr(
        gateway_cli, "launchd_plist_is_current", lambda app_wrapper=None: False
    )
    monkeypatch.setattr(
        gateway_cli, "generate_launchd_plist", lambda app_wrapper=False: temp_plist
    )
    monkeypatch.setattr(
        gateway_cli,
        "_refuse_temp_home_service_write",
        _REAL_REFUSE_TEMP_HOME_SERVICE_WRITE,
    )
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "launchctl must not run after temporary-HOME refusal"
        ),
    )
    monkeypatch.setattr(
        gateway_cli,
        "terminate_pid",
        lambda *_args, **_kwargs: pytest.fail(
            "gateway process must not be mutated after temporary-HOME refusal"
        ),
    )
    monkeypatch.setattr(
        "gateway.status.get_running_pid",
        lambda *_args, **_kwargs: pytest.fail(
            "restart must fail before probing or mutating the gateway process"
        ),
    )
    return plist_path, old_bytes


def _source_metadata(display_name: str) -> dict[str, object]:
    return {
        "SourcePython": "/temporary/python3",
        "SourceSize": 123,
        "SourceMTimeNs": 456,
        "DisplayName": display_name,
        "SigningIdentity": "-",
    }


def _write_wrapper(app_path: Path, *, info: Any, source_info: Any) -> None:
    contents = app_path / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    (
        contents / "Resources" / gateway_cli.MACOS_APP_WRAPPER_SOURCE_INFO
    ).write_bytes(plistlib.dumps(source_info))
    if isinstance(info, dict) and isinstance(info.get("CFBundleExecutable"), str):
        (contents / "MacOS" / info["CFBundleExecutable"]).write_bytes(b"python")


def test_launchd_plist_commit_is_atomic_and_fsyncs_file_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    replacements: list[tuple[Path, Path]] = []
    fsync_calls: list[int] = []
    real_replace = os.replace

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(gateway_cli.os, "replace", recording_replace)
    monkeypatch.setattr(gateway_cli.os, "fsync", lambda fd: fsync_calls.append(fd))

    gateway_cli._write_launchd_plist_with_pending_marker(plist_path, "new plist\n")

    assert plist_path.read_bytes() == b"new plist\n"
    assert [destination for _source, destination in replacements] == [
        gateway_cli._launchd_reload_pending_path(plist_path),
        plist_path,
    ]
    assert all(source.parent == destination.parent for source, destination in replacements)
    # Marker file+directory, then plist file+directory.
    assert len(fsync_calls) == 4


def test_failed_launchd_plist_commit_preserves_old_bytes_and_pending_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    old_bytes = b"old plist bytes\x00must remain byte-identical"
    plist_path.write_bytes(old_bytes)
    real_replace = os.replace

    def fail_plist_replace(source, destination):
        if Path(destination) == plist_path:
            raise OSError("simulated interrupted plist commit")
        return real_replace(source, destination)

    monkeypatch.setattr(gateway_cli.os, "replace", fail_plist_replace)

    with pytest.raises(OSError, match="simulated interrupted"):
        gateway_cli._write_launchd_plist_with_pending_marker(
            plist_path, "replacement\n"
        )

    assert plist_path.read_bytes() == old_bytes
    assert gateway_cli._launchd_reload_pending_path(plist_path).exists()
    assert not list(tmp_path.glob(f".{plist_path.name}.*.tmp"))


def test_pending_marker_stat_error_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    marker = gateway_cli._launchd_reload_pending_path(plist_path)
    original_stat = Path.stat

    def failing_marker_stat(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("marker stat denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing_marker_stat)
    assert gateway_cli._launchd_reload_is_pending(plist_path) is True


def test_reload_bootout_bootstrap_probe_and_sleep_share_one_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    plist_path.write_text("plist\n")
    now = {"value": 100.0}
    deadline = 130.0
    observed: list[tuple[str, float, float]] = []

    monkeypatch.setattr(gateway_cli, "_get_restart_drain_timeout", lambda: 1.0)
    monkeypatch.setattr(gateway_cli.time, "monotonic", lambda: now["value"])

    def fake_run(command, **kwargs):
        timeout = float(kwargs["timeout"])
        remaining = deadline - now["value"]
        observed.append((command[1], timeout, remaining))
        assert 0 < timeout <= remaining
        if command[1] == "bootout":
            now["value"] = 125.0
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[1] == "bootstrap":
            now["value"] = 128.0
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[1] == "list":
            now["value"] = 130.0
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        raise AssertionError(f"unexpected launchctl command: {command}")

    monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        gateway_cli.time,
        "sleep",
        lambda seconds: pytest.fail(f"sleep({seconds}) would cross the deadline"),
    )

    assert gateway_cli._reload_launchd_plist_now(plist_path) is False
    assert [operation for operation, _timeout, _remaining in observed] == [
        "bootout",
        "bootstrap",
        "list",
    ]
    assert gateway_cli._launchd_reload_pending_path(plist_path).exists()


def test_bootstrap_zero_but_unregistered_is_reload_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    plist_path.write_text("plist\n")
    now = {"value": 0.0}
    calls: list[list[str]] = []

    monkeypatch.setattr(gateway_cli, "_get_restart_drain_timeout", lambda: 1.0)
    monkeypatch.setattr(gateway_cli.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        gateway_cli.time,
        "sleep",
        lambda seconds: now.__setitem__("value", now["value"] + seconds),
    )

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[1] == "list":
            now["value"] = 30.0
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

    with pytest.raises(gateway_cli.LaunchdReloadError, match="did not register"):
        gateway_cli._require_launchd_reload(plist_path)

    assert [command[1] for command in calls] == ["bootout", "bootstrap", "list"]
    assert gateway_cli._launchd_reload_pending_path(plist_path).exists()


def test_launchd_start_does_not_kickstart_after_reload_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    plist_path.write_text("plist\n")
    target = "gui/501/ai.hermes.gateway"
    calls: list[list[str]] = []

    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        gateway_cli, "_resolve_launchd_app_wrapper_mode", lambda _value=None: False
    )
    monkeypatch.setattr(
        gateway_cli, "refresh_launchd_plist_if_needed", lambda **_kwargs: False
    )

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command == ["launchctl", "kickstart", target]:
            raise subprocess.CalledProcessError(3, command, stderr="not loaded")
        raise AssertionError(f"unexpected command after failed kickstart: {command}")

    monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        gateway_cli,
        "_require_launchd_reload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            gateway_cli.LaunchdReloadError("reload did not register")
        ),
    )

    with pytest.raises(gateway_cli.LaunchdReloadError, match="did not register"):
        gateway_cli.launchd_start()

    assert calls == [["launchctl", "kickstart", target]]


def test_gateway_tree_refresh_writes_marker_without_launchctl_or_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    plist_path.write_text("old\n")
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        gateway_cli, "_resolve_launchd_app_wrapper_mode", lambda _value=None: False
    )
    monkeypatch.setattr(
        gateway_cli, "launchd_plist_is_current", lambda app_wrapper=None: False
    )
    monkeypatch.setattr(
        gateway_cli,
        "generate_launchd_plist",
        lambda app_wrapper=False: "new\n",
    )
    monkeypatch.setattr(
        gateway_cli, "_is_running_inside_gateway_process_tree", lambda: True
    )
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("launchctl must not run"),
    )
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("launchctl submit must not run"),
    )

    assert gateway_cli.refresh_launchd_plist_if_needed() is True
    assert plist_path.read_text() == "new\n"
    assert gateway_cli._launchd_reload_pending_path(plist_path).exists()


def test_pending_restart_unsupported_domain_uses_one_exhausted_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    plist_path.write_text("plist\n")
    gateway_cli._mark_launchd_reload_pending(plist_path)
    reload_calls: list[tuple[Path, str | None, bool]] = []
    fallbacks: list[str] = []

    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        gateway_cli, "_resolve_launchd_app_wrapper_mode", lambda _value=None: False
    )
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)

    def exhausted(path, label=None, *, check_bootstrap=False):
        reload_calls.append((path, label, check_bootstrap))
        raise subprocess.CalledProcessError(
            125, ["launchctl", "bootstrap", "gui/501", str(path)]
        )

    monkeypatch.setattr(gateway_cli, "_require_launchd_reload", exhausted)
    monkeypatch.setattr(
        gateway_cli, "_launchd_fallback_to_detached", fallbacks.append
    )
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "no kickstart or second bootstrap may follow exhausted reload"
        ),
    )

    gateway_cli.launchd_restart()

    assert reload_calls == [(plist_path, "ai.hermes.gateway", True)]
    assert fallbacks == ["launchctl pending reload exit 125"]


@pytest.mark.parametrize("reload_state", ["stale", "pending"])
@pytest.mark.parametrize("returncode", [5, 125])
def test_launchd_start_reload_unsupported_domain_uses_detached_fallback(
    reload_state: str,
    returncode: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    plist_path.write_text("old plist\n")
    if reload_state == "pending":
        gateway_cli._mark_launchd_reload_pending(plist_path)
    required_calls: list[tuple[Path, str | None, bool]] = []
    fallbacks: list[str] = []

    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        gateway_cli, "_resolve_launchd_app_wrapper_mode", lambda _value=None: False
    )
    monkeypatch.setattr(
        gateway_cli,
        "launchd_plist_is_current",
        lambda app_wrapper=None: reload_state == "pending",
    )
    monkeypatch.setattr(
        gateway_cli, "generate_launchd_plist", lambda app_wrapper=False: "new plist\n"
    )

    def unsupported(path, label=None, *, check_bootstrap=False):
        required_calls.append((path, label, check_bootstrap))
        raise subprocess.CalledProcessError(
            returncode, ["launchctl", "bootstrap", "gui/501", str(path)]
        )

    monkeypatch.setattr(gateway_cli, "_require_launchd_reload", unsupported)
    monkeypatch.setattr(
        gateway_cli, "_launchd_fallback_to_detached", fallbacks.append
    )
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "kickstart must not follow an exhausted stale/pending reload"
        ),
    )

    gateway_cli.launchd_start()

    assert required_calls == [
        (plist_path, gateway_cli.get_launchd_label(), True)
    ]
    assert len(fallbacks) == 1
    assert f"exit {returncode}" in fallbacks[0]
    assert gateway_cli._launchd_reload_pending_path(plist_path).exists()
    assert "✓ Service started" not in capsys.readouterr().out


@pytest.mark.parametrize("returncode", [5, 125])
def test_launchd_non_force_install_stale_reload_uses_detached_fallback(
    returncode: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    plist_path.write_text("old plist\n")
    required_calls: list[tuple[Path, str | None, bool]] = []
    fallbacks: list[str] = []

    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        gateway_cli, "_resolve_launchd_app_wrapper_mode", lambda _value=None: False
    )
    monkeypatch.setattr(
        gateway_cli, "launchd_plist_is_current", lambda app_wrapper=None: False
    )
    monkeypatch.setattr(
        gateway_cli, "generate_launchd_plist", lambda app_wrapper=False: "new plist\n"
    )

    def unsupported(path, label=None, *, check_bootstrap=False):
        required_calls.append((path, label, check_bootstrap))
        raise subprocess.CalledProcessError(
            returncode, ["launchctl", "bootstrap", "gui/501", str(path)]
        )

    monkeypatch.setattr(gateway_cli, "_require_launchd_reload", unsupported)
    monkeypatch.setattr(
        gateway_cli, "_launchd_fallback_to_detached", fallbacks.append
    )

    gateway_cli.launchd_install(force=False)

    assert required_calls == [
        (plist_path, gateway_cli.get_launchd_label(), True)
    ]
    assert len(fallbacks) == 1
    assert f"exit {returncode}" in fallbacks[0]
    assert plist_path.read_text() == "new plist\n"
    assert gateway_cli._launchd_reload_pending_path(plist_path).exists()
    assert "✓ Service definition updated" not in capsys.readouterr().out


def test_launchd_start_temp_home_refusal_fails_without_mutating_installed_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    old_bytes = b"installed plist bytes\n"
    plist_path.write_bytes(old_bytes)
    temp_plist = plistlib.dumps(
        {"EnvironmentVariables": {"HERMES_HOME": "/tmp/hermes-contract-home"}}
    ).decode("utf-8")

    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        gateway_cli, "_resolve_launchd_app_wrapper_mode", lambda _value=None: False
    )
    monkeypatch.setattr(
        gateway_cli, "launchd_plist_is_current", lambda app_wrapper=None: False
    )
    monkeypatch.setattr(
        gateway_cli, "generate_launchd_plist", lambda app_wrapper=False: temp_plist
    )
    monkeypatch.setattr(
        gateway_cli,
        "_refuse_temp_home_service_write",
        _REAL_REFUSE_TEMP_HOME_SERVICE_WRITE,
    )
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "launchctl must not run after temporary-HOME refusal"
        ),
    )

    with pytest.raises(gateway_cli.LaunchdReloadError, match="temporary HERMES_HOME"):
        gateway_cli.launchd_start()

    assert plist_path.read_bytes() == old_bytes
    assert not gateway_cli._launchd_reload_pending_path(plist_path).exists()
    assert "✓ Service started" not in capsys.readouterr().out


def test_launchd_install_temp_home_refusal_fails_without_transaction_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    old_bytes = b"installed plist bytes\n"
    plist_path.write_bytes(old_bytes)
    temp_plist = plistlib.dumps(
        {"EnvironmentVariables": {"HERMES_HOME": "/tmp/hermes-contract-home"}}
    ).decode("utf-8")

    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        gateway_cli, "_resolve_launchd_app_wrapper_mode", lambda _value=None: False
    )
    monkeypatch.setattr(
        gateway_cli, "generate_launchd_plist", lambda app_wrapper=False: temp_plist
    )
    monkeypatch.setattr(
        gateway_cli,
        "_refuse_temp_home_service_write",
        _REAL_REFUSE_TEMP_HOME_SERVICE_WRITE,
    )
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "launchctl must not run after temporary-HOME refusal"
        ),
    )

    with pytest.raises(gateway_cli.LaunchdReloadError, match="temporary HERMES_HOME"):
        gateway_cli.launchd_install(force=True)

    assert plist_path.read_bytes() == old_bytes
    assert not gateway_cli._launchd_reload_pending_path(plist_path).exists()
    assert "✓ Service installed" not in capsys.readouterr().out


def test_gateway_restart_cli_propagates_temp_home_refusal_as_non_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plist_path, old_bytes = _stale_temp_home_restart(tmp_path, monkeypatch)
    _macos_gateway_cli(monkeypatch, plist_path)

    with pytest.raises(SystemExit) as exc_info:
        gateway_cli.gateway_command(
            SimpleNamespace(gateway_command="restart", all=False, system=False)
        )

    assert exc_info.value.code == 1
    assert plist_path.read_bytes() == old_bytes
    assert not gateway_cli._launchd_reload_pending_path(plist_path).exists()
    output = capsys.readouterr().out
    assert "temporary HERMES_HOME" in output
    assert "✓ Service restarted" not in output


def test_launchd_service_manager_propagates_temp_home_restart_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plist_path, old_bytes = _stale_temp_home_restart(tmp_path, monkeypatch)

    with pytest.raises(gateway_cli.LaunchdReloadError, match="temporary HERMES_HOME"):
        LaunchdServiceManager().restart("ignored")

    assert plist_path.read_bytes() == old_bytes
    assert not gateway_cli._launchd_reload_pending_path(plist_path).exists()
    assert "✓ Service restarted" not in capsys.readouterr().out


def test_launchd_stop_propagates_surviving_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(gateway_cli, "_wait_for_gateway_exit", lambda **_kwargs: False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda **_kwargs: None)

    assert gateway_cli.launchd_stop() is False
    output = capsys.readouterr().out.lower()
    assert "service stopped" not in output
    assert "still running" in output


def test_launchd_uninstall_keeps_plist_when_stop_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    old_bytes = b"installed plist\n"
    plist_path.write_bytes(old_bytes)
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gateway_cli, "launchd_stop", lambda: False)

    with pytest.raises(gateway_cli.LaunchdStopError, match="still running"):
        gateway_cli.launchd_uninstall()

    assert plist_path.read_bytes() == old_bytes
    assert not gateway_cli._launchd_reload_pending_path(plist_path).exists()


def test_launchd_uninstall_fsyncs_removal_before_clearing_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    marker_path = gateway_cli._launchd_reload_pending_path(plist_path)
    plist_path.write_text("installed plist\n")
    observations: list[tuple[bool, bool]] = []
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gateway_cli, "launchd_stop", lambda: True)
    monkeypatch.setattr(
        gateway_cli,
        "_fsync_directory",
        lambda _path: observations.append((plist_path.exists(), marker_path.exists())),
    )

    gateway_cli.launchd_uninstall()

    assert observations == [(True, True), (False, True), (False, False)]
    assert not plist_path.exists()
    assert not marker_path.exists()


@pytest.mark.parametrize("stop_all", [False, True])
def test_gateway_stop_cli_exits_nonzero_on_launchd_false(
    stop_all: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    plist_path.write_text("plist\n")
    _macos_gateway_cli(monkeypatch, plist_path)
    monkeypatch.setattr(gateway_cli, "launchd_stop", lambda: False)
    monkeypatch.setattr(
        gateway_cli,
        "stop_profile_gateway",
        lambda: (_ for _ in ()).throw(AssertionError("must not mask stop failure")),
    )
    monkeypatch.setattr(
        gateway_cli,
        "kill_gateway_processes",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not continue after stop failure")
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        gateway_cli.gateway_command(
            SimpleNamespace(gateway_command="stop", all=stop_all, system=False)
        )

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "stop failed" in output.lower()
    assert "✓ Stopped" not in output


def test_gateway_uninstall_cli_reports_stop_failure_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    old_bytes = b"installed plist\n"
    plist_path.write_bytes(old_bytes)
    _macos_gateway_cli(monkeypatch, plist_path)
    monkeypatch.setattr(gateway_cli, "launchd_stop", lambda: False)

    with pytest.raises(SystemExit) as exc_info:
        gateway_cli.gateway_command(
            SimpleNamespace(gateway_command="uninstall", system=False)
        )

    assert exc_info.value.code == 1
    output = capsys.readouterr().out.lower()
    assert "uninstall failed" in output
    assert "still running" in output
    assert "traceback" not in output
    assert plist_path.read_bytes() == old_bytes


def test_wrapper_discovery_requires_profile_bundle_metadata_executable_and_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper_dir = tmp_path / "home" / "macos"
    valid = wrapper_dir / "02-Amy Legacy.app"
    _write_wrapper(
        valid,
        info={
            "CFBundleExecutable": "Amy Legacy",
            "CFBundleIdentifier": "ai.hermes.gateway",
            "CFBundlePackageType": "APPL",
        },
        source_info=_source_metadata("Amy Legacy"),
    )
    wrong_profile = wrapper_dir / "00-Wrong Profile.app"
    _write_wrapper(
        wrong_profile,
        info={
            "CFBundleExecutable": "Wrong Profile",
            "CFBundleIdentifier": "ai.hermes.gateway-other",
            "CFBundlePackageType": "APPL",
        },
        source_info=_source_metadata("Wrong Profile"),
    )
    unsafe = wrapper_dir / "01-Unsafe.app"
    _write_wrapper(
        unsafe,
        info={
            "CFBundleExecutable": "../Unsafe",
            "CFBundleIdentifier": "ai.hermes.gateway",
            "CFBundlePackageType": "APPL",
        },
        source_info=_source_metadata("../Unsafe"),
    )
    monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: tmp_path / "home")
    monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: "ai.hermes.gateway")
    verified: list[Path] = []

    def verify(app_path=None):
        assert app_path is not None
        verified.append(Path(app_path))
        return app_path == valid

    monkeypatch.setattr(
        gateway_cli, "_launchd_app_wrapper_signature_is_valid", verify
    )

    assert gateway_cli._find_installed_launchd_app_wrapper() == valid
    assert verified == [valid]


def test_legacy_bundle_program_is_unknown_until_strict_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.hermes.gateway",
                "ProgramArguments": [
                    "/temporary/home/macos/Amy Legacy.app/Contents/MacOS/Amy Legacy",
                    "-m",
                    "hermes_cli.main",
                ],
                "EnvironmentVariables": {},
            }
        )
    )
    valid = tmp_path / "home" / "macos" / "Amy Legacy.app"
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: "ai.hermes.gateway")

    assert gateway_cli._installed_launchd_plist_app_wrapper_mode(plist_path) is None
    monkeypatch.setattr(
        gateway_cli, "_find_installed_launchd_app_wrapper", lambda: None
    )
    assert gateway_cli._resolve_launchd_app_wrapper_mode(None) is False
    monkeypatch.setattr(
        gateway_cli, "_find_installed_launchd_app_wrapper", lambda: valid
    )
    assert gateway_cli._resolve_launchd_app_wrapper_mode(None) is True


@pytest.mark.parametrize("plist_state", ["missing", "malformed", "pending"])
def test_wrapper_mode_recovery_preserves_discovered_wrapper(
    plist_state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    if plist_state == "malformed":
        plist_path.write_bytes(b"not a plist")
    elif plist_state == "pending":
        plist_path.write_bytes(
            plistlib.dumps(
                {
                    "Label": "ai.hermes.gateway",
                    "ProgramArguments": ["/usr/bin/python", "-m", "hermes_cli.main"],
                }
            )
        )
        gateway_cli._mark_launchd_reload_pending(plist_path)
    discovered = tmp_path / "home" / "macos" / "Amy Legacy.app"
    installs: list[bool] = []
    generated_modes: list[bool] = []

    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        gateway_cli, "_find_installed_launchd_app_wrapper", lambda: discovered
    )
    monkeypatch.setattr(gateway_cli, "launchd_app_wrapper_is_current", lambda: False)
    monkeypatch.setattr(
        gateway_cli,
        "install_launchd_app_wrapper",
        lambda force=False: installs.append(force) or discovered,
    )
    monkeypatch.setattr(
        gateway_cli,
        "launchd_plist_is_current",
        lambda app_wrapper=None: False,
    )
    monkeypatch.setattr(
        gateway_cli,
        "generate_launchd_plist",
        lambda app_wrapper=False: generated_modes.append(app_wrapper)
        or "wrapped plist\n",
    )
    monkeypatch.setattr(
        gateway_cli, "_is_running_inside_gateway_process_tree", lambda: True
    )
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("wrapper recovery must defer launchctl"),
    )

    if plist_state == "missing":
        gateway_cli.launchd_start()
    else:
        gateway_cli.launchd_start()

    assert installs == [True]
    assert generated_modes and set(generated_modes) == {True}
    assert gateway_cli._launchd_reload_pending_path(plist_path).exists()


def test_force_install_with_missing_plist_preserves_discovered_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    discovered = tmp_path / "home" / "macos" / "Amy.app"
    install_modes: list[bool] = []
    generated_modes: list[bool] = []

    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        gateway_cli, "_find_installed_launchd_app_wrapper", lambda: discovered
    )
    monkeypatch.setattr(gateway_cli, "launchd_app_wrapper_is_current", lambda: False)
    monkeypatch.setattr(
        gateway_cli,
        "install_launchd_app_wrapper",
        lambda force=False: install_modes.append(force) or discovered,
    )
    monkeypatch.setattr(
        gateway_cli,
        "generate_launchd_plist",
        lambda app_wrapper=False: generated_modes.append(app_wrapper)
        or "wrapped plist\n",
    )
    monkeypatch.setattr(
        gateway_cli, "_require_launchd_reload", lambda *_args, **_kwargs: None
    )

    gateway_cli.launchd_install(force=True)

    assert install_modes == [True]
    assert generated_modes == [True]


def test_existing_previous_survives_failed_followup_wrapper_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    source_python = tmp_path / "python3.13"
    source_python.write_bytes(b"new-python")
    app_path = home / "macos" / "Hermes Agent.app"
    app_executable = app_path / "Contents" / "MacOS" / "Hermes Agent"
    app_executable.parent.mkdir(parents=True)
    app_executable.write_bytes(b"current-python")
    backup_path = home / "macos" / ".Hermes Agent.app.previous"
    backup_executable = backup_path / "Contents" / "MacOS" / "Hermes Agent"
    backup_executable.parent.mkdir(parents=True)
    backup_executable.write_bytes(b"precious-previous-python")
    original_rename = Path.rename

    def fail_staging_commit(path, target):
        target = Path(target)
        if target == app_path and ".hermes-wrapper-" in str(path):
            raise OSError("commit failed")
        return original_rename(path, target)

    monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway_cli, "_profile_suffix", lambda: "")
    monkeypatch.setattr(gateway_cli, "get_python_path", lambda: str(source_python))
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(Path, "rename", fail_staging_commit)

    with pytest.raises(OSError, match="commit failed"):
        gateway_cli.install_launchd_app_wrapper(force=True)

    assert app_executable.read_bytes() == b"current-python"
    assert backup_executable.read_bytes() == b"precious-previous-python"


def test_generated_wrapper_plist_preserves_exact_logical_amy_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    physical_amy = tmp_path / "physical-amy"
    project = physical_amy / "hermes-agent"
    venv = project / ".venv"
    site_packages = venv / "lib" / "python3.13" / "site-packages"
    source_python = venv / "bin" / "python"
    inherited = tmp_path / "valid-inherited-bin"
    for directory in (site_packages, source_python.parent, inherited):
        directory.mkdir(parents=True, exist_ok=True)
    source_python.write_bytes(b"python")
    python_home = tmp_path / "cpython"
    (venv / "pyvenv.cfg").write_text(f"home = {python_home / 'bin'}\n")

    def logicalize(path):
        if path is None:
            return None
        candidate = Path(path)
        if candidate == Path("/srv/hermes"):
            return candidate
        try:
            return Path("/srv/hermes") / candidate.relative_to(physical_amy)
        except ValueError:
            return candidate

    expected_existing = {
        "/srv/hermes/hermes-agent/.venv/bin",
        "/srv/hermes/bin",
        "/srv/hermes/.go/bin",
        str(inherited),
        "/usr/bin",
        "/bin",
    }

    monkeypatch.setenv("HERMES_HOME", str(physical_amy))
    monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: physical_amy)
    monkeypatch.setattr(gateway_cli, "_profile_suffix", lambda: "")
    monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", project)
    monkeypatch.setattr(gateway_cli, "_detect_venv_dir", lambda: venv)
    monkeypatch.setattr(gateway_cli, "get_python_path", lambda: str(source_python))
    monkeypatch.setattr(gateway_cli, "_stable_service_working_dir", lambda: str(physical_amy))
    monkeypatch.setattr(gateway_cli, "_launchd_logical_hermes_path", logicalize)
    monkeypatch.setattr(gateway_cli, "_profile_arg", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(gateway_cli, "_build_service_path_dirs", lambda **_kwargs: [])
    monkeypatch.setattr(
        gateway_cli,
        "_launchd_path_entry_exists",
        lambda path: str(path) in expected_existing,
    )
    monkeypatch.setattr(gateway_cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        gateway_cli,
        "read_raw_config",
        lambda: {"gateway": {"macos_app_wrapper": {"display_name": "Amy"}}},
    )
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            [
                str(inherited),
                str(inherited),
                "/Applications/Codex.app/stale/bin",
                str(tmp_path / "missing-bin"),
            ]
        ),
    )

    plist = plistlib.loads(
        gateway_cli.generate_launchd_plist(app_wrapper=True).encode("utf-8")
    )

    assert plist["ProgramArguments"] == [
        "/srv/hermes/macos/Amy.app/Contents/MacOS/Amy",
        "-m",
        "hermes_cli.main",
        "gateway",
        "run",
        "--replace",
    ]
    assert plist["WorkingDirectory"] == "/srv/hermes"
    assert plist["StandardOutPath"] == "/srv/hermes/logs/gateway.log"
    assert plist["StandardErrorPath"] == "/srv/hermes/logs/gateway.error.log"
    assert plist["AssociatedBundleIdentifiers"] == "ai.hermes.gateway"
    assert plist["ThrottleInterval"] == 30
    assert plist["ExitTimeOut"] == 25
    assert plist["KeepAlive"] is True
    environment = plist["EnvironmentVariables"]
    assert environment["HERMES_HOME"] == "/srv/hermes"
    assert environment["VIRTUAL_ENV"] == "/srv/hermes/hermes-agent/.venv"
    assert environment["PYTHONEXECUTABLE"] == "/srv/hermes/hermes-agent/.venv/bin/python"
    assert environment["PYTHONPATH"].split(os.pathsep) == [
        "/srv/hermes/hermes-agent/.venv/lib/python3.13/site-packages",
        "/srv/hermes/hermes-agent",
    ]
    assert environment["PATH"].split(os.pathsep) == [
        "/srv/hermes/hermes-agent/.venv/bin",
        "/srv/hermes/bin",
        "/srv/hermes/.go/bin",
        str(inherited),
        "/usr/bin",
        "/bin",
    ]
    assert "/Applications/Codex.app/stale/bin" not in environment["PATH"]


def test_wrapper_discovery_skips_non_dict_metadata_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_identifier = "ai.hermes.test.wrapper"
    wrapper_dir = tmp_path / "home" / "macos"
    monkeypatch.setattr(
        gateway_cli, "get_launchd_bundle_identifier", lambda: bundle_identifier
    )
    _write_wrapper(
        wrapper_dir / "00-malformed-info.app",
        info=[],
        source_info=_source_metadata("bad"),
    )
    _write_wrapper(
        wrapper_dir / "01-malformed-source.app",
        info={
            "CFBundleExecutable": "Bad Source",
            "CFBundleIdentifier": bundle_identifier,
            "CFBundlePackageType": "APPL",
        },
        source_info=[],
    )
    valid = wrapper_dir / "02-valid.app"
    _write_wrapper(
        valid,
        info={
            "CFBundleExecutable": "Amy",
            "CFBundleIdentifier": bundle_identifier,
            "CFBundlePackageType": "APPL",
        },
        source_info=_source_metadata("Amy"),
    )
    monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: tmp_path / "home")
    monkeypatch.setattr(
        gateway_cli,
        "_launchd_app_wrapper_signature_is_valid",
        lambda app_path=None: True,
    )

    assert gateway_cli._find_installed_launchd_app_wrapper() == valid


@pytest.mark.parametrize("bad_metadata", ["info", "source"])
def test_current_wrapper_rejects_non_dict_metadata(
    bad_metadata: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = gateway_cli.MacOSAppWrapperIdentity("Amy", "-")
    app_path = tmp_path / "Amy.app"
    contents = app_path / "Contents"
    executable = contents / "MacOS" / "Amy"
    source_info_path = (
        contents / "Resources" / gateway_cli.MACOS_APP_WRAPPER_SOURCE_INFO
    )
    executable.parent.mkdir(parents=True)
    source_info_path.parent.mkdir(parents=True)
    executable.write_bytes(b"python")
    (contents / "Info.plist").write_bytes(
        plistlib.dumps([] if bad_metadata == "info" else {})
    )
    source_info_path.write_bytes(
        plistlib.dumps([] if bad_metadata == "source" else {})
    )
    source_python = tmp_path / "python3"
    source_python.write_bytes(b"source")
    monkeypatch.setattr(
        gateway_cli,
        "_resolve_launchd_app_wrapper_identity",
        lambda strict=False: identity,
    )
    monkeypatch.setattr(
        gateway_cli, "get_launchd_app_wrapper_path", lambda value=None: app_path
    )
    monkeypatch.setattr(
        gateway_cli,
        "get_launchd_app_wrapper_executable_path",
        lambda value=None: executable,
    )
    monkeypatch.setattr(gateway_cli, "get_python_path", lambda: str(source_python))
    monkeypatch.setattr(
        gateway_cli,
        "_launchd_app_wrapper_signature_is_valid",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("malformed metadata must fail before codesign")
        ),
    )

    assert gateway_cli.launchd_app_wrapper_is_current() is False


def test_gateway_restart_cli_renders_reload_exhaustion_as_non_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    plist_path.write_text("plist\n")
    _macos_gateway_cli(monkeypatch, plist_path)
    monkeypatch.setattr(
        gateway_cli,
        "launchd_restart",
        lambda: (_ for _ in ()).throw(
            gateway_cli.LaunchdReloadError("reload exhausted")
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        gateway_cli.gateway_command(
            SimpleNamespace(gateway_command="restart", all=False, system=False)
        )

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "reload exhausted" in output
    assert "✓ Service restarted" not in output


def test_launchd_restart_unloaded_recovery_uses_required_reload_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    plist_path.write_text("plist\n")
    target = f"gui/501/{gateway_cli.get_launchd_label()}"
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        gateway_cli, "_resolve_launchd_app_wrapper_mode", lambda value=None: False
    )
    monkeypatch.setattr(
        gateway_cli, "_launchd_reload_is_pending", lambda path=None: False
    )
    monkeypatch.setattr(
        gateway_cli, "launchd_plist_is_current", lambda app_wrapper=None: True
    )
    monkeypatch.setattr(gateway_cli, "_get_restart_drain_timeout", lambda: 1.0)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    run_calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        run_calls.append(command)
        if command == ["launchctl", "kickstart", "-k", target]:
            raise subprocess.CalledProcessError(3, command, stderr="not loaded")
        raise AssertionError(f"unexpected command after exhausted reload: {command}")

    monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)
    required_calls: list[tuple[Path, str | None, bool]] = []

    def required(path, label=None, *, check_bootstrap=False):
        required_calls.append((path, label, check_bootstrap))
        raise gateway_cli.LaunchdReloadError("unloaded recovery exhausted")

    monkeypatch.setattr(gateway_cli, "_require_launchd_reload", required)

    with pytest.raises(
        gateway_cli.LaunchdReloadError, match="unloaded recovery exhausted"
    ):
        gateway_cli.launchd_restart()

    assert required_calls == [(plist_path, gateway_cli.get_launchd_label(), True)]
    assert run_calls == [["launchctl", "kickstart", "-k", target]]


def test_launchd_uninstall_preserves_marker_when_plist_unlink_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    marker_path = gateway_cli._launchd_reload_pending_path(plist_path)
    plist_path.write_text("installed plist\n")
    fsync_calls = 0
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gateway_cli, "launchd_stop", lambda: True)

    def fail_plist_unlink_fsync(_path):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(gateway_cli, "_fsync_directory", fail_plist_unlink_fsync)

    with pytest.raises(OSError, match="directory fsync failure"):
        gateway_cli.launchd_uninstall()

    assert not plist_path.exists()
    assert marker_path.exists()
    assert "✓ Service uninstalled" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "error", [PermissionError("denied"), OSError("kill failed")]
)
def test_wait_for_gateway_exit_rechecks_and_fails_when_force_kill_is_denied(
    error: OSError, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_checks: list[int] = []

    def running_pid():
        pid_checks.append(123)
        return 123

    monkeypatch.setattr("gateway.status.get_running_pid", running_pid)
    monkeypatch.setattr(
        gateway_cli,
        "terminate_pid",
        lambda pid, force=False: (_ for _ in ()).throw(error),
    )

    assert gateway_cli._wait_for_gateway_exit(
        timeout=1.0, force_after=0.0
    ) is False
    assert len(pid_checks) >= 2
