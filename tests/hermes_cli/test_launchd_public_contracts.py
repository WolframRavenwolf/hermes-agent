"""Isolated P17 contracts at the native launchd entrypoints (no live service I/O)."""
import subprocess
from types import SimpleNamespace

import pytest

import hermes_cli.gateway as gw
from gateway import status

_native_retry = gw._retry_launchctl_bootstrap_until_registered
_native_bootstrap = gw._launchctl_bootstrap
_native_gateway_tree = gw._is_running_inside_gateway_process_tree


@pytest.fixture
def launchd(tmp_path, monkeypatch):
    plist = tmp_path / "ai.hermes.gateway.plist"
    plist.write_text("old definition\n")
    plist.chmod(0o640)
    calls = []
    monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gw, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gw, "get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr(gw, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(gw, "generate_launchd_plist", lambda **k: "new definition\n")
    monkeypatch.setattr(gw, "launchd_plist_is_current", lambda **k: False)
    monkeypatch.setattr(gw, "_resolve_launchd_app_wrapper_mode", lambda *a: False)
    monkeypatch.setattr(gw, "_is_running_inside_gateway_process_tree", lambda: False)
    monkeypatch.setattr(gw, "_refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr(gw, "_get_restart_drain_timeout", lambda: 0.0)
    monkeypatch.setattr(gw, "wait_for_launchd_gateway_supervision", lambda **k: True)
    monkeypatch.setattr(gw, "_retry_launchctl_bootstrap_until_registered", lambda *a, **k: True)
    monkeypatch.setattr(status, "get_running_pid", lambda **k: None)
    monkeypatch.setattr(gw.subprocess, "run", lambda cmd, **k: calls.append(cmd) or SimpleNamespace(returncode=0, stdout='', stderr=''))
    monkeypatch.setattr(gw.subprocess, "Popen", lambda *a, **k: pytest.fail("unmocked service submission"))
    return plist, calls


def test_install_failure_restores_prior_definition_and_mode(launchd, monkeypatch):
    plist, _ = launchd
    def fail(*a, **k):
        raise subprocess.CalledProcessError(78, "bootstrap")
    monkeypatch.setattr(gw, "_retry_launchctl_bootstrap_until_registered", fail)
    with pytest.raises(subprocess.CalledProcessError):
        gw.launchd_install(force=True)
    assert plist.read_text() == "old definition\n"
    assert plist.stat().st_mode & 0o777 == 0o640


def test_install_requires_observed_supervision_not_bootstrap_exit_zero(launchd, monkeypatch, capsys):
    monkeypatch.setattr(gw, "wait_for_launchd_gateway_supervision", lambda **k: False)
    with pytest.raises(RuntimeError, match="supervis"):
        gw.launchd_install(force=True)
    assert "installed and loaded" not in capsys.readouterr().out


def test_refresh_exhaustion_is_not_reported_as_success(launchd, monkeypatch, capsys):
    plist, _ = launchd
    monkeypatch.setattr(gw, "_retry_launchctl_bootstrap_until_registered", lambda *a, **k: False)
    with pytest.raises(RuntimeError, match="reload"):
        gw.refresh_launchd_plist_if_needed()
    assert "Updated gateway" not in capsys.readouterr().out
    assert gw._launchd_reload_pending_path(plist).exists()


def test_current_plist_with_pending_reload_is_retried(launchd, monkeypatch):
    plist, calls = launchd
    monkeypatch.setattr(gw, "_retry_launchctl_bootstrap_until_registered", lambda *a, **k: False)
    try:
        gw.refresh_launchd_plist_if_needed()
    except RuntimeError:
        pass
    calls.clear()
    monkeypatch.setattr(gw, "launchd_plist_is_current", lambda **k: True)
    monkeypatch.setattr(gw, "_retry_launchctl_bootstrap_until_registered", lambda *a, **k: True)
    assert gw.refresh_launchd_plist_if_needed() is True
    assert any(cmd[1] == "bootout" for cmd in calls)
    assert not gw._launchd_reload_pending_path(plist).exists()


def test_submit_failure_never_falls_back_into_gateway_coalition(launchd, monkeypatch):
    plist, calls = launchd
    monkeypatch.setattr(status, "get_running_pid", lambda **k: 424242)
    def fail_submit(*a, **k):
        raise OSError("submit failed")
    monkeypatch.setattr(gw.subprocess, "Popen", fail_submit)
    with pytest.raises(RuntimeError, match="submit|helper"):
        gw.refresh_launchd_plist_if_needed()
    assert not any(cmd[1] in {"bootout", "bootstrap"} for cmd in calls)
    assert plist.read_text() == "old definition\n"


def test_deferred_reload_retains_pending_until_positive_pid(launchd, monkeypatch):
    plist, calls = launchd
    monkeypatch.setattr(status, "get_running_pid", lambda **k: 424242)
    submitted = []
    monkeypatch.setattr(gw.subprocess, "Popen", lambda argv, **k: submitted.append(argv))
    assert gw.refresh_launchd_plist_if_needed() is True
    script = submitted[0][-1]
    assert submitted[0][:2] == ["launchctl", "submit"]
    assert gw._launchd_reload_pending_path(plist).exists()
    assert str(gw._launchd_reload_pending_path(plist)) in script
    assert '[1-9][0-9]*' in script
    assert not calls


@pytest.mark.parametrize("plist_state", ["stale", "current_pending"])
def test_start_leaves_pending_with_outstanding_reload_and_old_supervision(
    launchd, monkeypatch, capsys, plist_state
):
    from tools import process_registry

    plist, calls = launchd
    marker = gw._launchd_reload_pending_path(plist)
    if plist_state == "current_pending":
        monkeypatch.setattr(gw, "launchd_plist_is_current", lambda **kw: True)
        gw._mark_launchd_reload_pending(plist)
    monkeypatch.setattr(status, "get_running_pid", lambda **kw: 424242)
    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: False)
    submitted = []
    old_supervision_checks = []

    def submit_without_running(argv, **kwargs):
        # Record the durable marker, but leave the independent job before bootout.
        submitted.append((argv, marker.read_bytes()))
        return SimpleNamespace(returncode=None)

    def positive_old_supervision(**kwargs):
        old_supervision_checks.append(424242)
        return True

    monkeypatch.setattr(gw.subprocess, "Popen", submit_without_running)
    monkeypatch.setattr(gw, "wait_for_launchd_gateway_supervision", positive_old_supervision)

    gw.launchd_start()

    assert len(submitted) == 1
    assert submitted[0][0][:2] == ["launchctl", "submit"]
    assert plist.read_text() == "new definition\n"
    assert {
        "caller_operations": [cmd[1] for cmd in calls],
        "pending_preserved": marker.exists() and marker.read_bytes() == submitted[0][1],
        "old_supervision_checks": old_supervision_checks,
        "reported_started": "✓ Service started" in capsys.readouterr().out,
    } == {
        "caller_operations": [],
        "pending_preserved": True,
        "old_supervision_checks": [],
        "reported_started": False,
    }


def test_atomic_plist_write_does_not_modify_link_target(launchd, monkeypatch):
    plist, _ = launchd
    victim = plist.with_suffix('.victim')
    plist.rename(victim)
    plist.symlink_to(victim)
    with pytest.raises((OSError, RuntimeError), match="symlink|regular|owner"):
        gw.launchd_install(force=True)
    assert victim.read_text() == "old definition\n"


def test_bootstrap_retry_does_not_spend_time_after_deadline(launchd, monkeypatch):
    _, calls = launchd
    # Restore the native retry function that the isolated fixture usually stubs.
    monkeypatch.setattr(gw, "_retry_launchctl_bootstrap_until_registered", _native_retry)
    monkeypatch.setattr(gw.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(gw.subprocess, "run", lambda cmd, **k: calls.append(cmd) or SimpleNamespace(returncode=0, stdout='', stderr=''))
    monkeypatch.setattr(gw, "_append_launchd_reload_log", lambda *a: None)
    assert gw._retry_launchctl_bootstrap_until_registered('gui/501', '/synthetic.plist', 'ai.hermes.gateway', deadline=10.0) is False
    assert calls == []


def test_bootstrap_eio_recovery_shares_one_timeout(monkeypatch, tmp_path):
    now = [0.0]
    budgets = []
    def run(cmd, **kw):
        budgets.append(kw['timeout'])
        now[0] += 4.0
        if len(budgets) == 1:
            raise subprocess.CalledProcessError(5, cmd)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(gw.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(gw.subprocess, "run", run)
    gw._launchctl_bootstrap('gui/501', tmp_path / 'job.plist', 'ai.hermes.gateway', timeout=10)
    assert budgets == [10, 6, 2]


def test_force_stop_permission_error_is_failure_not_success(monkeypatch):
    monkeypatch.setattr(status, "get_running_pid", lambda **k: 424242)
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: 100)
    monkeypatch.setattr(gw.time, "monotonic", lambda: 0.0)
    def denied(*a, **k):
        raise PermissionError('not owner')
    monkeypatch.setattr(gw, "terminate_pid", denied)
    assert gw._wait_for_gateway_exit(timeout=1, force_after=0) is False


def test_force_stop_never_signals_replacement_pid(monkeypatch):
    pids = iter([424242, 434343])
    monkeypatch.setattr(status, "get_running_pid", lambda **k: next(pids, 434343))
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: 100)
    now = iter([0, 0, 0, 1, 2, 3, 4])
    monkeypatch.setattr(gw.time, "monotonic", lambda: next(now, 4))
    monkeypatch.setattr(gw.time, "sleep", lambda *a: None)
    killed = []
    monkeypatch.setattr(gw, "terminate_pid", lambda pid, **k: killed.append(pid))
    assert gw._wait_for_gateway_exit(timeout=2, force_after=0) is False
    assert killed == []


def test_stop_surfaces_wait_failure(launchd, monkeypatch, capsys):
    monkeypatch.setattr(gw, "_wait_for_gateway_exit", lambda **k: False)
    assert gw.launchd_stop() is False
    assert "Service stopped" not in capsys.readouterr().out


def test_uninstall_keeps_definition_if_stop_failed(launchd, monkeypatch):
    plist, _ = launchd
    monkeypatch.setattr(gw, "launchd_stop", lambda: False)
    with pytest.raises(RuntimeError, match="stop"):
        gw.launchd_uninstall()
    assert plist.exists()


def test_service_manager_must_surface_failed_stop(monkeypatch):
    from hermes_cli.service_manager import LaunchdServiceManager
    monkeypatch.setattr(gw, "launchd_stop", lambda: False)
    with pytest.raises(RuntimeError, match="stop"):
        LaunchdServiceManager().stop('gateway')


def test_refresh_bootout_and_retry_share_deadline(launchd, monkeypatch):
    _, calls = launchd
    now = [0.0]
    def run(cmd, **kw):
        calls.append((cmd, kw['timeout']))
        now[0] += 5.0
        return SimpleNamespace(returncode=0)
    seen_deadlines = []
    monkeypatch.setattr(gw.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(gw.subprocess, "run", run)
    monkeypatch.setattr(gw, "_retry_launchctl_bootstrap_until_registered", lambda *a, **kw: seen_deadlines.append(kw['deadline']) or True)
    gw.refresh_launchd_plist_if_needed()
    assert calls[0][1] <= 30
    assert seen_deadlines == [30]


@pytest.mark.parametrize('missing', [False, True])
def test_start_waits_for_supervision_even_after_successful_launchctl(launchd, monkeypatch, missing):
    plist, _ = launchd
    if missing:
        plist.unlink()
    monkeypatch.setattr(gw, 'refresh_launchd_plist_if_needed', lambda **kw: False)
    monkeypatch.setattr(gw, 'wait_for_launchd_gateway_supervision', lambda **kw: False)
    with pytest.raises(RuntimeError, match='supervis'):
        gw.launchd_start()
    if missing:
        assert gw._launchd_reload_pending_path(plist).exists()


def test_restart_does_not_race_pending_native_reload(launchd, monkeypatch):
    _, calls = launchd
    monkeypatch.setattr(gw, 'refresh_launchd_plist_if_needed', lambda **kw: True)
    gw.launchd_restart()
    assert calls == []


def test_install_descendant_persists_definition_without_bootstrap(launchd, monkeypatch):
    plist, calls = launchd
    monkeypatch.setattr(gw, '_is_running_inside_gateway_process_tree', lambda: True)
    gw.launchd_install(force=True)
    assert calls == []
    assert gw._launchd_reload_pending_path(plist).exists()


def test_start_descendant_does_not_kickstart_after_deferred_refresh(launchd, monkeypatch):
    _, calls = launchd
    monkeypatch.setattr(gw, '_is_running_inside_gateway_process_tree', lambda: True)
    monkeypatch.setattr(gw, 'refresh_launchd_plist_if_needed', lambda **kw: True)
    gw.launchd_start()
    assert calls == []


def test_stop_with_unknown_start_identity_never_force_kills(monkeypatch):
    monkeypatch.setattr(status, 'get_running_pid', lambda **kw: 424242)
    monkeypatch.setattr(status, 'get_process_start_time', lambda pid: None)
    now = iter([0, 0, 0, 0, 1, 2, 3])
    monkeypatch.setattr(gw.time, 'monotonic', lambda: next(now, 3))
    monkeypatch.setattr(gw.time, 'sleep', lambda *a: None)
    killed = []
    monkeypatch.setattr(gw, 'terminate_pid', lambda pid, **kw: killed.append(pid))
    assert gw._wait_for_gateway_exit(timeout=2, force_after=0) is False
    assert killed == []


@pytest.mark.parametrize('fail_swap', [True, False])
def test_existing_previous_survives_failed_followup_wrapper_swap(tmp_path, monkeypatch, fail_swap):
    from pathlib import Path
    source = tmp_path / 'python3'
    source.write_bytes(b'new-python')
    home = tmp_path / 'home'
    app = home / 'macos' / 'Hermes Agent.app'
    previous = app.with_name('.Hermes Agent.app.previous')
    executable = Path('Contents/MacOS/Hermes Agent')
    for root, contents in [(app, b'current-python'), (previous, b'precious-previous-python')]:
        (root / executable).parent.mkdir(parents=True)
        (root / executable).write_bytes(contents)
    real_rename = Path.rename
    def rename(path, target):
        if Path(target) == app and '.hermes-wrapper-' in str(path):
            if fail_swap:
                raise OSError('commit failed')
            assert (previous / executable).read_bytes() == b'precious-previous-python'
        return real_rename(path, target)
    monkeypatch.setattr(gw, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(gw, 'read_raw_config', lambda: {})
    monkeypatch.setattr(gw, '_profile_suffix', lambda: '')
    monkeypatch.setattr(gw, '_detect_venv_dir', lambda: None)
    monkeypatch.setattr(gw, 'get_python_path', lambda: str(source))
    monkeypatch.setattr(gw.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='', stderr=''))
    monkeypatch.setattr(Path, 'rename', rename)
    if fail_swap:
        with pytest.raises(OSError, match='commit failed'):
            gw.install_launchd_app_wrapper(force=True)
        assert (app / executable).read_bytes() == b'current-python'
        assert (previous / executable).read_bytes() == b'precious-previous-python'
    else:
        assert gw.install_launchd_app_wrapper(force=True) == app
        assert (app / executable).read_bytes() == b'new-python'
        assert not previous.exists()
    assert not list(app.parent.glob('.Hermes Agent.app.rollback-*'))


@pytest.mark.parametrize('handoff_delay', [0.0, 0.25])
@pytest.mark.parametrize('eio', [False, True])
def test_reload_bootout_bootstrap_probe_and_sleep_share_one_deadline(launchd, monkeypatch, handoff_delay, eio):
    plist, _ = launchd
    now = [100.0]
    deadline = 130.0
    observed = []
    monkeypatch.setattr(gw.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(gw.time, 'sleep', lambda seconds: pytest.fail('sleep would cross deadline'))
    monkeypatch.setattr(gw, '_retry_launchctl_bootstrap_until_registered', _native_retry)
    def bootstrap(*args, **kw):
        now[0] += handoff_delay
        return _native_bootstrap(*args, **kw)
    monkeypatch.setattr(gw, '_launchctl_bootstrap', bootstrap)
    def run(command, **kw):
        assert 0 < kw['timeout'] <= deadline - now[0]
        operation = command[1]
        observed.append(operation)
        if len(observed) == 1:
            now[0] = 125.0
        elif operation == 'bootstrap':
            now[0] += 1.0
            if eio and observed.count('bootstrap') == 1:
                raise subprocess.CalledProcessError(5, command)
        elif operation == 'bootout':
            now[0] += 1.0
        elif operation == 'list':
            now[0] = deadline
        else:
            pytest.fail(f'unexpected command: {command}')
        return SimpleNamespace(returncode=1 if operation == 'list' else 0, stdout='', stderr='')
    monkeypatch.setattr(gw.subprocess, 'run', run)
    with pytest.raises(gw.LaunchdReloadError, match='reload failed'):
        gw.refresh_launchd_plist_if_needed()
    assert observed == (['bootout', 'bootstrap', 'bootout', 'bootstrap', 'list'] if eio else ['bootout', 'bootstrap', 'list'])
    assert gw._launchd_reload_pending_path(plist).exists()


@pytest.mark.parametrize('membership', ['descendant', 'supervised', 'marker_only'])
def test_refresh_gateway_tree_persists_only_but_external_reload_still_submits(launchd, monkeypatch, capsys, membership):
    from tools import process_registry
    plist, calls = launchd
    submitted = []
    monkeypatch.setenv('_HERMES_GATEWAY', '1')
    monkeypatch.setattr(gw, '_is_running_inside_gateway_process_tree', _native_gateway_tree)
    monkeypatch.setattr(status, 'get_running_pid', lambda **kw: 424242)
    monkeypatch.setattr(gw, '_get_parent_pid', lambda pid: 424242 if membership == 'descendant' and pid == gw.os.getpid() else 0)
    monkeypatch.setattr(process_registry, '_is_supervised_gateway_process', lambda: membership == 'supervised')
    monkeypatch.setattr(gw.subprocess, 'Popen', lambda argv, **kw: submitted.append(argv))
    assert gw.refresh_launchd_plist_if_needed() is True
    assert plist.read_text() == 'new definition\n'
    assert gw._launchd_reload_pending_path(plist).exists()
    assert calls == []
    if membership == 'marker_only':
        assert len(submitted) == 1
        assert submitted[0][:2] == ['launchctl', 'submit']
    else:
        assert submitted == []
        assert 'external shell' in capsys.readouterr().out


# Original 7f07d524 preservation regressions, adapted to the native owner.
import os
import plistlib
from pathlib import Path
from typing import Any
from hermes_cli.service_manager import LaunchdServiceManager

_native_wrapper_mode = gw._resolve_launchd_app_wrapper_mode
gateway_cli = gw
_REAL_REFUSE_TEMP_HOME_SERVICE_WRITE = gw._refuse_temp_home_service_write


@pytest.fixture
def historical_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gw, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gw, "_launchd_user_home", lambda: home)
    monkeypatch.setattr(gw, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(gw, "_is_running_inside_gateway_process_tree", lambda: False)
    monkeypatch.setattr(gw, "_refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr(gw, "_clear_launchd_unsupported_marker", lambda: None)
    monkeypatch.setattr(gw, "read_raw_config", lambda: {})
    monkeypatch.setattr(gw.subprocess, "run", lambda *a, **k: pytest.fail("unexpected service/signature I/O"))
    monkeypatch.setattr(gw.subprocess, "Popen", lambda *a, **k: pytest.fail("unexpected submission"))
    return home

def _macos_gateway_cli(monkeypatch: pytest.MonkeyPatch, plist_path: Path) -> None:
    monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
    monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)
    monkeypatch.setattr(gateway_cli, "is_windows", lambda: False)
    monkeypatch.setattr(gateway_cli, "is_container", lambda: False)
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: False)
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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

    with pytest.raises(gw.LaunchdReloadError, match="simulated interrupted"):
        gateway_cli._write_launchd_plist_with_pending_marker(
            plist_path, "replacement\n"
        )

    assert plist_path.read_bytes() == old_bytes
    assert gateway_cli._launchd_reload_pending_path(plist_path).exists()
    assert not list(tmp_path.glob(f".{plist_path.name}.*.tmp"))


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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
        plistlib.dumps([] if bad_metadata == "info" else gateway_cli._launchd_app_wrapper_info(identity))
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
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


@pytest.mark.usefixtures("historical_home")
@pytest.mark.parametrize("wrapped", [False, True])
def test_historical_logical_paths_preserve_native_supervisor(tmp_path, monkeypatch, wrapped):
    physical = tmp_path / "physical"
    logical = tmp_path / "logical"
    physical.mkdir()
    logical.symlink_to(physical, target_is_directory=True)
    project = physical / "hermes-agent"
    venv = project / ".venv"
    site = venv / "lib/python3.11/site-packages"
    inherited = tmp_path / "inherited-bin"
    for directory in [site, venv / "bin", physical / "bin", physical / ".go/bin", inherited]:
        directory.mkdir(parents=True, exist_ok=True)
    python = venv / "bin/python"
    python.write_bytes(b"fixture")
    (venv / "pyvenv.cfg").write_text(f"home = {tmp_path}/cpython/bin\n")
    monkeypatch.setenv("HERMES_HOME", str(logical))
    monkeypatch.setattr(gw, "get_hermes_home", lambda: logical)
    monkeypatch.setattr(gw, "PROJECT_ROOT", project)
    monkeypatch.setattr(gw, "_detect_venv_dir", lambda: venv)
    monkeypatch.setattr(gw, "get_python_path", lambda: str(python))
    monkeypatch.setattr(gw, "_stable_service_working_dir", lambda: str(physical))
    monkeypatch.setattr(gw, "_profile_suffix", lambda: "")
    monkeypatch.setattr(gw, "_build_service_path_dirs", lambda **kw: [])
    monkeypatch.setattr(gw.shutil, "which", lambda *a: None)
    monkeypatch.setenv("PATH", os.pathsep.join([str(inherited), str(physical / 'bin'), str(inherited), '/Applications/Codex.app/stale/bin', str(tmp_path / 'missing')]))
    data = plistlib.loads(gw.generate_launchd_plist(app_wrapper=wrapped).encode())
    env = data['EnvironmentVariables']
    assert env['HERMES_HOME'] == str(logical)
    assert env['VIRTUAL_ENV'] == str(logical / 'hermes-agent/.venv')
    assert data['WorkingDirectory'] == str(logical)
    assert data['StandardOutPath'] == str(logical / 'logs/gateway.log')
    assert data['StandardErrorPath'] == str(logical / 'logs/gateway.error.log')
    args = data['ProgramArguments']
    exe = logical / ('macos/Hermes Agent.app/Contents/MacOS/Hermes Agent' if wrapped else 'hermes-agent/.venv/bin/python')
    assert args[0] == args[args.index('--') + 1] == str(exe)
    assert args[args.index('--error-log') + 1] == data['StandardErrorPath']
    assert args[1:3] == ['-m', 'hermes_cli.stderr_timestamp']
    assert '--external-supervisor' in args and '--replace' not in args
    assert env['HERMES_SUPERVISED_CHILD'] == '1'
    assert env['PATH'].split(os.pathsep)[:4] == [str(logical / 'hermes-agent/.venv/bin'), str(logical / 'bin'), str(logical / '.go/bin'), str(inherited)]
    assert env['PATH'].split(os.pathsep).count(str(logical / 'bin')) == 1
    assert '/Applications/Codex.app/stale/bin' not in env['PATH']
    assert str(tmp_path / 'missing') not in env['PATH']
    if wrapped:
        assert env['PYTHONEXECUTABLE'] == str(logical / 'hermes-agent/.venv/bin/python')
        assert env['PYTHONPATH'].split(os.pathsep) == [str(logical / site.relative_to(physical)), str(logical / 'hermes-agent')]
    assert data['KeepAlive'] is True and data['ThrottleInterval'] == 30 and data['ExitTimeOut'] == 25


@pytest.mark.parametrize('action', ['start', 'install', 'restart'])
@pytest.mark.parametrize('returncode', [5, 125])
def test_historical_stale_reload_unsupported_falls_back_once(launchd, monkeypatch, action, returncode):
    plist, calls = launchd
    now = [0.0]
    monkeypatch.setattr(gw.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(gw.time, 'sleep', lambda sec: now.__setitem__(0, now[0] + sec))
    monkeypatch.setattr(gw, '_retry_launchctl_bootstrap_until_registered', _native_retry)
    monkeypatch.setattr(gw, '_append_launchd_reload_log', lambda *a: None)
    def bootstrap(*a, **kw):
        now[0] = 30.0
        raise subprocess.CalledProcessError(returncode, 'bootstrap')
    monkeypatch.setattr(gw, '_launchctl_bootstrap', bootstrap)
    fallbacks = []
    monkeypatch.setattr(gw, '_launchd_fallback_to_detached', fallbacks.append)
    getattr(gw, 'launchd_' + action)()
    assert len(fallbacks) == 1 and f'exit {returncode}' in fallbacks[0]
    assert [c[1] for c in calls] == ['bootout']
    assert gw._launchd_reload_pending_path(plist).exists()


def test_historical_unloaded_restart_requires_observed_supervision(launchd, monkeypatch, capsys):
    plist, calls = launchd
    monkeypatch.setattr(gw, 'refresh_launchd_plist_if_needed', lambda **kw: False)
    monkeypatch.setattr(gw, 'wait_for_launchd_gateway_supervision', lambda **kw: False)
    def run(cmd, **kw):
        calls.append(cmd)
        if '-k' in cmd:
            raise subprocess.CalledProcessError(3, cmd)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(gw.subprocess, 'run', run)
    with pytest.raises(gw.LaunchdReloadError):
        gw.launchd_restart()
    assert '✓ Service restarted' not in capsys.readouterr().out
    assert gw._launchd_reload_pending_path(plist).exists()


def test_historical_status_reports_pending_even_when_current(launchd, monkeypatch, capsys):
    plist, _ = launchd
    gw._launchd_reload_pending_path(plist).write_text('pending')
    monkeypatch.setattr(gw, 'launchd_plist_is_current', lambda **kw: True)
    gw.launchd_status()
    assert 'reload is pending' in capsys.readouterr().out


@pytest.mark.parametrize('state', ['missing', 'raw', 'pending'])
def test_historical_force_install_keeps_discovery_but_raw_is_authoritative(launchd, monkeypatch, state):
    plist, _ = launchd
    mode = gw._resolve_launchd_app_wrapper_mode
    # Use the unmocked resolver; capture it separately because launchd fixture stubs it.
    monkeypatch.setattr(gw, '_resolve_launchd_app_wrapper_mode', _native_wrapper_mode)
    if state == 'missing':
        plist.unlink()
    else:
        plist.write_bytes(plistlib.dumps({'Label': 'ai.hermes.gateway', 'ProgramArguments': ['/usr/bin/python']}))
        if state == 'pending':
            gw._launchd_reload_pending_path(plist).write_text('pending')
    discovered = plist.parent / 'macos/Legacy.app'
    monkeypatch.setattr(gw, '_find_installed_launchd_app_wrapper', lambda: discovered, raising=False)
    installs = []
    monkeypatch.setattr(gw, 'install_launchd_app_wrapper', lambda force=False: installs.append(force))
    monkeypatch.setattr(gw, 'launchd_app_wrapper_is_current', lambda: False)
    modes = []
    monkeypatch.setattr(gw, 'generate_launchd_plist', lambda app_wrapper=False: modes.append(app_wrapper) or 'new definition\n')
    monkeypatch.setattr(gw, '_launchctl_bootstrap', lambda *a, **kw: None)
    gw.launchd_install(force=True)
    assert installs == ([] if state == 'raw' else [True])
    assert modes == [state != 'raw']


def test_historical_force_error_rechecks_for_already_gone_process(monkeypatch):
    pids = iter([424242, 424242, None])
    monkeypatch.setattr(status, 'get_running_pid', lambda **kw: next(pids, None))
    monkeypatch.setattr(status, 'get_process_start_time', lambda pid: 100)
    monkeypatch.setattr(gw.time, 'monotonic', lambda: 0.0)
    def denied(*a, **kw):
        raise PermissionError('gone during signal')
    monkeypatch.setattr(gw, 'terminate_pid', denied)
    assert gw._wait_for_gateway_exit(timeout=1, force_after=0) is True



def test_historical_restart_descendant_never_kickstarts(launchd, monkeypatch):
    monkeypatch.setattr(gw, 'refresh_launchd_plist_if_needed', lambda **kw: False)
    monkeypatch.setattr(gw, '_is_running_inside_gateway_process_tree', lambda: True)
    with pytest.raises(gw.LaunchdReloadError, match='inside'):
        gw.launchd_restart()
    assert launchd[1] == []


def test_historical_bootout_timeout_still_attempts_remaining_reload(launchd, monkeypatch):
    plist, _ = launchd
    now = [0.0]
    monkeypatch.setattr(gw.time, 'monotonic', lambda: now[0])
    def run(cmd, **kw):
        now[0] = 5.0
        raise subprocess.TimeoutExpired(cmd, kw['timeout'])
    monkeypatch.setattr(gw.subprocess, 'run', run)
    deadlines = []
    monkeypatch.setattr(gw, '_retry_launchctl_bootstrap_until_registered', lambda *a, **kw: deadlines.append(kw['deadline']) or True)
    assert gw.refresh_launchd_plist_if_needed() is True
    assert deadlines == [30.0]
    assert not gw._launchd_reload_pending_path(plist).exists()


@pytest.mark.parametrize('entry', ['install', 'start_missing', 'start_unloaded', 'restart_unloaded'])
def test_historical_sync_recovery_verifies_before_kickstart_with_one_budget(launchd, monkeypatch, entry):
    plist, calls = launchd
    if entry == 'start_missing':
        plist.unlink()
    monkeypatch.setattr(gw, 'refresh_launchd_plist_if_needed', lambda **kw: False)
    monkeypatch.setattr(gw, '_retry_launchctl_bootstrap_until_registered', _native_retry)
    now = [0.0]
    monkeypatch.setattr(gw.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(gw.time, 'sleep', lambda sec: now.__setitem__(0, now[0] + sec))
    monkeypatch.setattr(gw, '_append_launchd_reload_log', lambda *a: None)
    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == 'kickstart' and len(calls) == 1 and entry.endswith('unloaded'):
            raise subprocess.CalledProcessError(3, cmd)
        if cmd[1] == 'bootstrap':
            now[0] = 30.0
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(gw.subprocess, 'run', run)
    with pytest.raises(gw.LaunchdReloadError):
        if entry == 'install':
            gw.launchd_install(force=True)
        elif entry.startswith('start'):
            gw.launchd_start()
        else:
            gw.launchd_restart()
    assert sum(cmd[1] == 'kickstart' for cmd in calls) == int(entry.endswith('unloaded'))
    assert gw._launchd_reload_pending_path(plist).exists()


@pytest.mark.usefixtures("historical_home")
@pytest.mark.parametrize('bad', ['profile', 'package', 'exe', 'display', 'source', 'size_bool', 'size_negative', 'mtime_bool', 'mtime_negative', 'identity', 'signature'])
def test_historical_wrapper_discovery_rejects_each_invalid_component(historical_home, monkeypatch, bad):
    info = {'CFBundleExecutable': 'Legacy', 'CFBundleIdentifier': gw.get_launchd_label(), 'CFBundlePackageType': 'APPL'}
    source = _source_metadata('Legacy')
    if bad == 'profile': info['CFBundleIdentifier'] = 'other.profile'
    if bad == 'package': info['CFBundlePackageType'] = 'BNDL'
    if bad == 'display': source['DisplayName'] = 'Other'
    if bad == 'source': source['SourcePython'] = ''
    if bad == 'size_bool': source['SourceSize'] = True
    if bad == 'size_negative': source['SourceSize'] = -1
    if bad == 'mtime_bool': source['SourceMTimeNs'] = True
    if bad == 'mtime_negative': source['SourceMTimeNs'] = -1
    if bad == 'identity': source['SigningIdentity'] = ''
    app = historical_home / 'macos/Legacy.app'
    _write_wrapper(app, info=info, source_info=source)
    if bad == 'exe': (app / 'Contents/MacOS/Legacy').unlink()
    verified = []
    monkeypatch.setattr(gw, '_launchd_app_wrapper_signature_is_valid', lambda path: verified.append(path) or False)
    assert gw._find_installed_launchd_app_wrapper() is None
    assert verified == ([app] if bad == 'signature' else [])


@pytest.mark.usefixtures("historical_home")
@pytest.mark.parametrize('data', [[], {'Label': 'other'}, {'Label': 'ai.hermes.gateway', 'ProgramArguments': [1]}, {'Label': 'ai.hermes.gateway', 'Program': 1}, {'Label': 'ai.hermes.gateway', 'ProgramArguments': ['python'], 'EnvironmentVariables': []}])
def test_historical_plist_malformed_shapes_are_unknown(tmp_path, monkeypatch, data):
    plist = tmp_path / 'job.plist'
    plist.write_bytes(plistlib.dumps(data))
    monkeypatch.setattr(gw, 'get_launchd_label', lambda: 'ai.hermes.gateway')
    assert gw._installed_launchd_plist_app_wrapper_mode(plist) is None


def test_historical_explicit_wrapper_mode_does_not_probe(monkeypatch):
    monkeypatch.setattr(gw, 'get_launchd_plist_path', lambda: pytest.fail('explicit mode must not discover'))
    assert gw._resolve_launchd_app_wrapper_mode(True) is True
    assert gw._resolve_launchd_app_wrapper_mode(False) is False


def test_historical_pending_helpers_default_to_active_plist(launchd):
    plist, _ = launchd
    gw._mark_launchd_reload_pending()
    assert gw._launchd_reload_is_pending() is True
    assert gw._launchd_reload_pending_path() == gw._launchd_reload_pending_path(plist)
    gw._clear_launchd_reload_pending()
    assert gw._launchd_reload_is_pending() is False


@pytest.fixture
def recovery_service(launchd, monkeypatch):
    """Real publication/reload/retry owners; synthetic clock and launchctl only."""
    plist, calls = launchd
    service = SimpleNamespace(plist=plist, calls=calls, pid=5150, bootstrap_error=None,
                              marker=gw._launchd_reload_pending_path(plist), clock=0.0)
    monkeypatch.setattr(gw, "_retry_launchctl_bootstrap_until_registered", _native_retry)
    monkeypatch.setattr(gw, "_clear_launchd_unsupported_marker", lambda: None)
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: False)
    monkeypatch.setattr(gw.time, "monotonic", lambda: service.clock)
    monkeypatch.setattr(gw.time, "sleep", lambda seconds: setattr(service, "clock", service.clock + seconds))
    monkeypatch.setattr(gw, "launchd_plist_is_current", lambda **k: plist.exists() and plist.read_bytes() == b"new definition\n")

    def run(command, **kwargs):
        assert command[0] == "launchctl"
        assert command[1] in {"bootout", "bootstrap", "list", "kickstart"}
        calls.append(command)
        service.clock += 1
        if command[1] == "bootstrap" and service.bootstrap_error:
            raise subprocess.CalledProcessError(service.bootstrap_error, command, stderr="bootstrap refused")
        return SimpleNamespace(returncode=0, stdout=f'"PID" = {service.pid};', stderr="")

    monkeypatch.setattr(gw.subprocess, "run", run)
    for name in ("terminate_pid", "_spawn_detached_gateway", "install_launchd_app_wrapper"):
        monkeypatch.setattr(gw, name, lambda *a, **k: pytest.fail("unexpected actuation"))
    return service


@pytest.mark.parametrize("entry", ["refresh", "install"])
@pytest.mark.parametrize("point", ["replace", "directory_fsync", "activation"])
@pytest.mark.parametrize("missing", [False, True])
def test_c6_failed_update_restores_preimage(recovery_service, monkeypatch, entry, point, missing):
    import stat
    service = recovery_service
    if missing:
        service.plist.unlink()
        service.marker.write_text("prior failed install")
    replace, fsync = os.replace, os.fsync
    promoted = False
    injected = False

    def replace_checked(src, dst):
        nonlocal promoted, injected
        if Path(dst) == service.plist and not promoted:
            assert service.marker.exists()
            assert Path(src).parent == service.plist.parent
            if point == "replace":
                injected = True
                raise OSError("publication diagnostic")
            promoted = True
        return replace(src, dst)

    def fsync_checked(fd):
        nonlocal injected
        if point == "directory_fsync" and promoted and not injected and stat.S_ISDIR(os.fstat(fd).st_mode):
            injected = True
            raise OSError("publication diagnostic")
        return fsync(fd)

    monkeypatch.setattr(os, "replace", replace_checked)
    monkeypatch.setattr(os, "fsync", fsync_checked)
    if point == "activation":
        service.pid = 0
    with pytest.raises((OSError, gw.LaunchdReloadError), match="publication diagnostic|reload failed"):
        if entry == "install":
            gw.launchd_install(force=True)
        else:
            gw.refresh_launchd_plist_if_needed()
    assert service.plist.exists() is (not missing)
    if not missing:
        assert service.plist.read_bytes() == b"old definition\n"
        assert stat.S_IMODE(service.plist.stat().st_mode) == 0o640
    assert service.marker.exists()
    if point != "activation":
        assert injected
        assert not service.calls
    assert not list(service.plist.parent.glob(".*"))


@pytest.mark.parametrize("foreign", ["bytes", "inode", "mode", "pending"])
def test_c6_rollback_refuses_foreign_image(recovery_service, monkeypatch, foreign):
    service = recovery_service
    monkeypatch.setattr(status, "get_running_pid", lambda **k: 424242)
    expected = b"new definition\n"

    def replace_then_fail(*a, **k):
        nonlocal expected
        if foreign == "pending":
            service.marker.write_text("newer attempt")
        elif foreign == "mode":
            service.plist.chmod(0o600)
        else:
            replacement = service.plist.with_suffix(".other")
            expected = b"foreign" if foreign == "bytes" else expected
            replacement.write_bytes(expected)
            replacement.chmod(0o640)
            os.replace(replacement, service.plist)
        raise OSError("submission diagnostic")

    monkeypatch.setattr(gw.subprocess, "Popen", replace_then_fail)
    with pytest.raises(gw.LaunchdReloadError) as error:
        gw.refresh_launchd_plist_if_needed()
    assert "submission diagnostic" in str(error.value)
    assert "rollback" in str(error.value) and "owner changed" in str(error.value)
    assert service.plist.read_bytes() == expected
    assert service.plist.stat().st_mode & 0o777 == (0o600 if foreign == "mode" else 0o640)
    assert service.marker.exists()
    if foreign == "pending":
        assert service.marker.read_text() == "newer attempt"
    assert not service.calls


def test_c6_restore_failure_preserves_both_diagnostics(recovery_service, monkeypatch):
    service = recovery_service
    monkeypatch.setattr(status, "get_running_pid", lambda **k: 424242)
    replace = os.replace

    def fail_submit(*a, **k):
        raise OSError("submission diagnostic")

    def fail_restore(src, dst):
        if Path(dst) == service.plist and Path(src).read_bytes() == b"old definition\n":
            raise OSError("restoration diagnostic")
        return replace(src, dst)

    monkeypatch.setattr(gw.subprocess, "Popen", fail_submit)
    monkeypatch.setattr(os, "replace", fail_restore)
    with pytest.raises(gw.LaunchdReloadError) as error:
        gw.refresh_launchd_plist_if_needed()
    assert "submission diagnostic" in str(error.value)
    assert "restoration diagnostic" in str(error.value)
    assert service.marker.exists()
    assert service.plist.read_bytes() == b"new definition\n"


@pytest.mark.parametrize("entry", ["refresh", "install", "start", "restart", "updater"])
@pytest.mark.parametrize("missing", [False, True])
def test_c6_pending_retry_owns_activation(recovery_service, entry, missing):
    from hermes_cli.update_cmd import _restart_launchd_gateway_after_update
    service = recovery_service
    service.plist.write_bytes(b"new definition\n")
    service.marker.write_text("prior failed update")
    if missing:
        service.plist.unlink()
    if entry == "updater":
        assert _restart_launchd_gateway_after_update() == ([gw.get_launchd_label()], [])
    elif entry == "refresh":
        assert gw.refresh_launchd_plist_if_needed() is True
    else:
        getattr(gw, "launchd_" + entry)()
    assert service.plist.read_bytes() == b"new definition\n"
    assert not service.marker.exists()
    assert any(cmd[1] == "bootstrap" for cmd in service.calls)
    assert not any(cmd[1] == "kickstart" for cmd in service.calls)


@pytest.mark.parametrize("entry", ["restart", "restart_all", "updater"])
def test_c6_failed_install_public_retry_and_cancel(recovery_service, monkeypatch, entry):
    from hermes_cli.update_cmd import _restart_launchd_gateway_after_update
    service = recovery_service
    _macos_gateway_cli(monkeypatch, service.plist)
    monkeypatch.setattr(gw, "is_managed", lambda: False)
    service.plist.unlink()
    service.pid = 0
    with pytest.raises(gw.LaunchdReloadError):
        gw.launchd_install()
    assert not service.plist.exists() and service.marker.exists()
    service.calls.clear()
    service.pid = 5150
    manual = []
    monkeypatch.setattr(gw, "stop_profile_gateway", lambda: manual.append("stop") or False)
    monkeypatch.setattr(gw, "kill_gateway_processes", lambda **k: manual.append("kill-all") or 0)
    monkeypatch.setattr(gw, "_wait_for_gateway_exit", lambda **k: manual.append("wait") or True)
    monkeypatch.setattr(gw, "run_gateway", lambda **k: manual.append("run"))

    def retry():
        if entry == "updater":
            return _restart_launchd_gateway_after_update()
        gw.gateway_command(SimpleNamespace(gateway_command="restart", all=entry == "restart_all"))

    result = retry()
    if entry == "updater":
        assert result == ([gw.get_launchd_label()], [])
    assert manual == []
    assert service.plist.exists() and not service.marker.exists()
    assert any(cmd[1] == "bootstrap" for cmd in service.calls)
    # Recreate the failed-install state, then revoke its pending-only authority.
    service.plist.unlink()
    service.marker.write_text("failed install")
    monkeypatch.setattr(gw, "launchd_stop", lambda: True)
    gw.launchd_uninstall()
    assert not service.marker.exists()
    service.calls.clear()
    result = retry()
    assert not service.calls and not service.plist.exists()
    if entry == "updater":
        assert result == ([], []) and manual == []
    else:
        assert manual == ["kill-all" if entry == "restart_all" else "stop", "wait", "run"]


@pytest.mark.parametrize("stopped", [False, True])
def test_c6_pending_only_uninstall_requires_stop(recovery_service, monkeypatch, stopped):
    service = recovery_service
    service.plist.unlink()
    service.marker.write_text("failed install")
    monkeypatch.setattr(gw, "launchd_stop", lambda: stopped)
    if stopped:
        gw.launchd_uninstall()
    else:
        with pytest.raises(gw.LaunchdStopError):
            gw.launchd_uninstall()
    assert service.marker.exists() is (not stopped)
    assert not service.plist.exists()


@pytest.mark.parametrize("point", ["unlink", "directory_fsync"])
def test_c6_pending_cancellation_failure_is_loud(recovery_service, monkeypatch, capsys, point):
    service = recovery_service
    service.plist.unlink()
    service.marker.write_text("failed install")
    unlink = Path.unlink
    monkeypatch.setattr(gw, "launchd_stop", lambda: True)

    def fail_unlink(path, *a, **k):
        if path == service.marker:
            raise OSError("cancellation diagnostic")
        return unlink(path, *a, **k)

    def fail_sync(path):
        raise OSError("cancellation diagnostic")

    if point == "unlink":
        monkeypatch.setattr(Path, "unlink", fail_unlink)
    else:
        monkeypatch.setattr(gw, "_fsync_directory", fail_sync)
    with pytest.raises((OSError, gw.LaunchdReloadError), match="cancellation diagnostic"):
        gw.launchd_uninstall()
    assert "Service uninstalled" not in capsys.readouterr().out
    assert service.marker.exists() is (point == "unlink")


@pytest.mark.parametrize("failure", ["activation", "refusal"])
def test_c6_current_refresh_failure_continues_siblings(recovery_service, monkeypatch, capsys, failure):
    from hermes_cli.update_cmd import _restart_macos_launchd_gateways
    service = recovery_service
    current = gw.get_launchd_label()
    sibling = current + "-sibling"
    service.pid = 0
    if failure == "refusal":
        monkeypatch.setattr(gw, "_refuse_temp_home_service_write", lambda *a: True)
    monkeypatch.setattr(gw, "launchd_gateway_labels_for_install", lambda: [current, sibling])
    monkeypatch.setattr(gw, "_locate_launchd_gateway_service", lambda label: ("user/501", None))
    monkeypatch.setattr(gw, "_wait_for_launchd_service_pid", lambda *a, **k: True)
    restarted, failed = [], []
    _restart_macos_launchd_gateways(restarted, failed, 0)
    assert failed == [current] and restarted == [sibling]
    assert ["launchctl", "kickstart", "-k", "user/501/" + sibling] in service.calls
    assert service.plist.read_bytes() == b"old definition\n"
    output = capsys.readouterr().out
    assert "hermes gateway restart" in output
    assert ("temporary HERMES_HOME" if failure == "refusal" else "reload failed") in output
