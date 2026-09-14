"""Launchd publication and activation contracts, with OS/service boundaries isolated."""

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.gateway as gateway
import hermes_cli.gateway_launchd_reload as launchd_reload

pytestmark = pytest.mark.macos_only


@pytest.fixture
def service(tmp_path, monkeypatch):
    plist = tmp_path / "ai.hermes.gateway-test.plist"
    plist.write_bytes(b"old definition")
    plist.chmod(0o640)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.hermes.gateway-test")
    monkeypatch.setattr(gateway, "generate_launchd_plist", lambda: "new definition")
    monkeypatch.setattr(gateway, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(gateway, "_launchd_reload_budget", lambda: 0)
    monkeypatch.setattr(gateway, "_refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(gateway, "get_python_path", lambda: sys.executable)
    calls = []
    state = SimpleNamespace(pid=5150, bootstrap_error=None, calls=calls, plist=plist,
                            marker=plist.with_name(plist.name + ".reload-pending"))

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "bootstrap" and state.bootstrap_error:
            raise subprocess.CalledProcessError(state.bootstrap_error, cmd, stderr="bootstrap denied")
        return SimpleNamespace(returncode=0, stdout=f'"PID" = {state.pid};', stderr="")

    monkeypatch.setattr(gateway.subprocess, "run", run)
    monkeypatch.setattr(gateway.subprocess, "Popen", lambda *a, **k: pytest.fail("unexpected helper"))
    return state


@pytest.mark.parametrize("failure", ["activation", "unsafe_pending"])
def test_refresh_failure_does_not_skip_sibling_gateways(service, monkeypatch, capsys, failure):
    from hermes_cli.update_cmd_fleet import _restart_macos_launchd_gateways

    service.pid = 0
    if failure == "unsafe_pending":
        service.plist.unlink()
        service.marker.write_text("unsafe pending marker")
        service.marker.chmod(0o666)
    current = gateway.get_launchd_label()
    sibling = current + "-sibling"
    monkeypatch.setattr(gateway, "launchd_gateway_labels_for_install", lambda: [current, sibling])
    monkeypatch.setattr(gateway, "_locate_launchd_gateway_service", lambda label: ("gui/501", None))
    monkeypatch.setattr(gateway, "_wait_for_launchd_service_pid", lambda *a, **k: True)
    restarted, failed = [], []
    _restart_macos_launchd_gateways(restarted, failed, 0)
    assert failed == [current]
    assert restarted == [sibling]
    assert any(cmd[1] == "kickstart" and cmd[-1] == "gui/501/" + sibling for cmd in service.calls)
    assert service.marker.exists()
    assert "hermes gateway restart" in capsys.readouterr().out


def test_failed_submit_preserves_preimage_without_bootout(service, monkeypatch):
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 4242)

    def fail(*args, **kwargs):
        raise OSError("submit unavailable")

    monkeypatch.setattr(gateway.subprocess, "Popen", fail)
    with pytest.raises(launchd_reload.LaunchdReloadError, match="submit unavailable"):
        gateway.refresh_launchd_plist_if_needed()
    assert service.plist.read_bytes() == b"old definition"
    assert stat.S_IMODE(service.plist.stat().st_mode) == 0o640
    assert not service.calls
    assert service.marker.exists()


@pytest.mark.parametrize("point", ["replace", "directory_fsync"])
def test_publication_failure_restores_old_bytes_and_mode(service, monkeypatch, point):
    replace = os.replace
    fsync = os.fsync
    promoted = False

    def replace_checked(src, dst):
        nonlocal promoted
        if Path(dst) == service.plist:
            assert service.marker.is_file(), "pending must precede publication"
            assert Path(src).parent == service.plist.parent
            if not promoted:
                promoted = True
                if point == "replace":
                    raise OSError("publication failed")
        return replace(src, dst)

    def fsync_checked(fd):
        nonlocal promoted
        if point == "directory_fsync" and promoted and stat.S_ISDIR(os.fstat(fd).st_mode):
            promoted = False
            raise OSError("publication failed")
        return fsync(fd)

    monkeypatch.setattr(os, "replace", replace_checked)
    monkeypatch.setattr(os, "fsync", fsync_checked)
    with pytest.raises(launchd_reload.LaunchdReloadError, match="publication failed"):
        gateway.refresh_launchd_plist_if_needed()
    assert service.plist.read_bytes() == b"old definition"
    assert stat.S_IMODE(service.plist.stat().st_mode) == 0o640
    assert service.marker.exists()
    assert not service.calls
    assert not list(service.plist.parent.glob(".*.tmp"))


@pytest.mark.parametrize("foreign", ["different_bytes", "same_bytes_new_inode", "mode_change"])
def test_rollback_refuses_foreign_replacement(service, monkeypatch, foreign):
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 4242)

    def replace_then_fail(*args, **kwargs):
        if foreign == "mode_change":
            service.plist.chmod(0o600)
        else:
            other = service.plist.with_suffix(".other")
            other.write_bytes(b"foreign" if foreign == "different_bytes" else b"new definition")
            other.chmod(0o640)
            os.replace(other, service.plist)
        raise OSError("submit failed")

    monkeypatch.setattr(gateway.subprocess, "Popen", replace_then_fail)
    with pytest.raises(launchd_reload.LaunchdReloadError) as error:
        gateway.refresh_launchd_plist_if_needed()
    assert "submit failed" in str(error.value) and "rollback" in str(error.value)
    assert "owner changed" in str(error.value)
    assert service.plist.read_bytes() == (b"foreign" if foreign == "different_bytes" else b"new definition")
    assert service.marker.exists()
    assert not service.calls


def test_restore_failure_keeps_both_diagnostics(service, monkeypatch):
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 4242)
    replace = os.replace

    def fail_submit(*a, **k):
        raise OSError("submission diagnostic")

    def fail_restore(src, dst):
        if Path(dst) == service.plist and Path(src).read_bytes() == b"old definition":
            raise OSError("restoration diagnostic")
        return replace(src, dst)

    monkeypatch.setattr(gateway.subprocess, "Popen", fail_submit)
    monkeypatch.setattr(os, "replace", fail_restore)
    with pytest.raises(launchd_reload.LaunchdReloadError) as error:
        gateway.refresh_launchd_plist_if_needed()
    assert "submission diagnostic" in str(error.value)
    assert "restoration diagnostic" in str(error.value)
    assert service.marker.exists()


@pytest.mark.parametrize("invalid", ["symlink", "dangling", "directory", "writable", "foreign_owner"])
def test_unsafe_plist_is_rejected_before_mutation(service, monkeypatch, invalid):
    if invalid in ("symlink", "dangling", "directory"):
        service.plist.unlink()
        if invalid == "directory":
            service.plist.mkdir()
        else:
            target = service.plist.with_suffix(".target")
            if invalid == "symlink":
                target.write_bytes(b"unrelated")
            service.plist.symlink_to(target)
    elif invalid == "writable":
        service.plist.chmod(0o666)
    else:
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    with pytest.raises(launchd_reload.LaunchdReloadError):
        gateway.refresh_launchd_plist_if_needed()
    assert not service.calls
    assert not service.marker.exists()


@pytest.mark.parametrize("entry", ["refresh_launchd_plist_if_needed", "launchd_start", "launchd_restart", "launchd_install"])
@pytest.mark.parametrize("missing", [False, True])
def test_pending_or_missing_definition_retries_activation(service, entry, missing):
    service.plist.write_bytes(b"new definition")
    service.marker.write_text("previous failed attempt")
    if missing:
        service.plist.unlink()
    getattr(gateway, entry)()
    assert [c for c in service.calls if c[1] == "bootstrap"]
    assert not [c for c in service.calls if c[1] == "kickstart"]
    assert service.plist.read_bytes() == b"new definition"
    assert not service.marker.exists()


@pytest.mark.parametrize("entry", ["refresh_launchd_plist_if_needed", "launchd_start", "launchd_restart", "launchd_install"])
@pytest.mark.parametrize("pid", [0, -1])
def test_unobserved_activation_retains_pending_and_preimage(service, entry, pid, capsys):
    service.pid = pid
    service.plist.write_bytes(b"new definition")
    service.marker.write_text("prior attempt")
    with pytest.raises(launchd_reload.LaunchdReloadError):
        getattr(gateway, entry)()
    assert service.marker.exists()
    assert service.plist.read_bytes() == b"new definition"
    assert stat.S_IMODE(service.plist.stat().st_mode) == 0o640
    assert "Service started" not in capsys.readouterr().out


@pytest.mark.parametrize("pid", [0, 5150])
def test_initial_install_requires_observed_supervision(service, pid, capsys):
    service.plist.unlink()
    service.pid = pid
    if pid == 0:
        with pytest.raises(launchd_reload.LaunchdReloadError):
            gateway.launchd_install()
        assert service.marker.exists()
        assert not service.plist.exists()
        assert "installed and loaded" not in capsys.readouterr().out
    else:
        gateway.launchd_install()
        assert not service.marker.exists()
        assert stat.S_IMODE(service.plist.stat().st_mode) == 0o600
        assert "installed and loaded" in capsys.readouterr().out


@pytest.mark.parametrize("entry", ["launchd_start", "launchd_restart", "launchd_install"])
@pytest.mark.parametrize("code", [5, 125])
def test_terminal_bootstrap_error_reaches_native_detached_fallback(service, monkeypatch, entry, code):
    service.bootstrap_error = code
    detached = []
    monkeypatch.setattr(gateway, "_spawn_detached_gateway", lambda: detached.append(True) or True)
    getattr(gateway, entry)()
    assert detached == [True]
    assert service.marker.exists()
    assert gateway._launchd_unsupported_marker_exists()
    assert service.plist.read_bytes() == b"old definition"
    if code == 5:
        assert len([c for c in service.calls if c[1] == "bootstrap"]) >= 2


@pytest.mark.parametrize("ownership", ["absent", "newer", "unreadable"])
@pytest.mark.parametrize("entry", ["refresh_launchd_plist_if_needed", "launchd_install"])
def test_completion_requires_matching_pending_authority(service, monkeypatch, capsys, ownership, entry):
    def run(cmd, **kw):
        if cmd[1] == "list":
            if ownership == "absent":
                service.marker.unlink()
            elif ownership == "newer":
                service.marker.write_text("newer attempt")
            else:
                service.marker.chmod(0o666)
        return SimpleNamespace(returncode=0, stdout='"PID" = 5150;', stderr="")

    monkeypatch.setattr(gateway.subprocess, "run", run)
    with pytest.raises(launchd_reload.LaunchdReloadError):
        if entry == "launchd_install":
            gateway.launchd_install(force=True)
        else:
            gateway.refresh_launchd_plist_if_needed()
    # Lost authority can neither claim completion nor compensate this image.
    assert service.plist.read_bytes() == b"new definition"
    if ownership == "newer":
        assert service.marker.read_text() == "newer attempt"
    output = capsys.readouterr().out
    assert "installed and loaded" not in output
    assert "to match the current Hermes install" not in output


@pytest.fixture
def public_service(service, monkeypatch):
    monkeypatch.setattr(gateway, "is_macos", lambda: True)
    monkeypatch.setattr(gateway, "is_windows", lambda: False)
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway, "_systemd_unit_installed", lambda: False)
    monkeypatch.setattr(gateway, "is_managed", lambda: False)
    monkeypatch.setattr(gateway, "is_termux", lambda: False)
    monkeypatch.setattr(gateway, "_dispatch_via_service_manager_if_s6", lambda *a: False)
    monkeypatch.setattr(gateway, "_dispatch_all_via_service_manager_if_s6", lambda *a: False)
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: False)
    return service


@pytest.fixture
def failed_initial_install(public_service, monkeypatch):
    service = public_service
    service.plist.unlink()
    service.pid = 0
    with pytest.raises(SystemExit) as error:
        gateway.gateway_command(SimpleNamespace(gateway_command="install"))
    assert error.value.code == 1
    assert not service.plist.exists()
    assert service.marker.is_file()
    service.calls.clear()

    def no_manual_fallback(*args, **kwargs):
        pytest.fail("pending launchd recovery entered manual stop/wait/run fallback")

    for name in ("stop_profile_gateway", "kill_gateway_processes", "_wait_for_gateway_exit", "run_gateway"):
        monkeypatch.setattr(gateway, name, no_manual_fallback)
    return service


@pytest.mark.parametrize("entry", ["restart", "restart_all", "updater"])
@pytest.mark.parametrize("pid", [0, 5150])
def test_public_retry_consumes_failed_install_state(failed_initial_install, entry, pid):
    from hermes_cli.update_cmd_fleet import _restart_launchd_gateway_after_update

    service = failed_initial_install
    service.pid = pid
    if entry == "updater":
        if pid == 0:
            assert _restart_launchd_gateway_after_update() == ([], [gateway.get_launchd_label()])
        else:
            assert _restart_launchd_gateway_after_update() == ([gateway.get_launchd_label()], [])
    else:
        args = SimpleNamespace(gateway_command="restart", all=entry == "restart_all")
        if pid == 0:
            with pytest.raises(SystemExit) as error:
                gateway.gateway_command(args)
            assert error.value.code == 1
        else:
            gateway.gateway_command(args)
    assert [c for c in service.calls if c[1] == "bootstrap"]
    assert not [c for c in service.calls if c[1] == "kickstart"]
    assert service.marker.exists() is (pid == 0)
    assert service.plist.exists() is (pid > 0)
    if pid > 0:
        assert service.plist.read_bytes() == b"new definition"


@pytest.mark.parametrize("restart_all", [False, True])
def test_public_restart_without_definition_or_pending_keeps_native_fallback(public_service, monkeypatch, restart_all):
    public_service.plist.unlink()
    calls = []
    monkeypatch.setattr(gateway, "stop_profile_gateway", lambda: calls.append("stop") or False)
    monkeypatch.setattr(gateway, "kill_gateway_processes", lambda **kw: calls.append("kill-all") or 0)
    monkeypatch.setattr(gateway, "_wait_for_gateway_exit", lambda **kw: calls.append("wait") or True)
    monkeypatch.setattr(gateway, "run_gateway", lambda **kw: calls.append("run"))
    gateway.gateway_command(SimpleNamespace(gateway_command="restart", all=restart_all))
    assert calls == ["kill-all" if restart_all else "stop", "wait", "run"]
    assert not public_service.calls


@pytest.mark.parametrize("entry", ["launchd_install", "refresh_launchd_plist_if_needed"])
@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("point", ["unlink", "directory_fsync"])
def test_retirement_failure_respects_observed_activation_boundary(service, monkeypatch, capsys, entry, missing, point):
    if missing:
        service.plist.unlink()
    unlink = Path.unlink
    fsync = os.fsync
    activated_image = None
    retired = False
    injected = False

    def unlink_checked(path, *args, **kwargs):
        nonlocal activated_image, retired, injected
        if path == service.marker:
            assert [c for c in service.calls if c[1] == "list"]
            assert service.pid > 0
            activated_image = service.plist.stat()
            if point == "unlink":
                injected = True
                raise OSError("marker unlink failed")
            result = unlink(path, *args, **kwargs)
            retired = True
            return result
        return unlink(path, *args, **kwargs)

    def fsync_checked(fd):
        nonlocal injected
        if retired and not injected and stat.S_ISDIR(os.fstat(fd).st_mode):
            injected = True
            raise OSError("retirement directory sync failed")
        return fsync(fd)

    monkeypatch.setattr(Path, "unlink", unlink_checked)
    monkeypatch.setattr(os, "fsync", fsync_checked)
    if point == "unlink":
        with pytest.raises(launchd_reload.LaunchdReloadError, match="marker unlink failed"):
            getattr(gateway, entry)()
        assert service.marker.exists()
        assert service.plist.exists() is (not missing)
        if not missing:
            assert service.plist.read_bytes() == b"old definition"
            assert stat.S_IMODE(service.plist.stat().st_mode) == 0o640
        assert "activation succeeded" not in capsys.readouterr().err
    else:
        getattr(gateway, entry)()
        assert not service.marker.exists()
        assert service.plist.read_bytes() == b"new definition"
        current = service.plist.stat()
        assert activated_image is not None
        assert (current.st_dev, current.st_ino, current.st_mode) == (
            activated_image.st_dev, activated_image.st_ino, activated_image.st_mode)
        warning = capsys.readouterr().err
        assert "activation succeeded" in warning
        assert "pending-marker directory sync failed" in warning
        assert "durability" in warning
    assert injected


@pytest.mark.parametrize("missing", [False, True])
def test_rollback_directory_sync_failure_still_propagates(service, monkeypatch, missing):
    if missing:
        service.plist.unlink()
    service.pid = 0
    fsync = os.fsync

    def fsync_checked(fd):
        if any(c[1] == "bootstrap" for c in service.calls) and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("rollback directory sync failed")
        return fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_checked)
    with pytest.raises(launchd_reload.LaunchdReloadError, match="rollback failed: rollback directory sync failed"):
        gateway.refresh_launchd_plist_if_needed()
    assert service.marker.exists()


# Execute the generated native helper with shell functions at every service/clock
# boundary. The child Python only retires a marker inside the sandbox temp tree.
_REAL_RUN = subprocess.run
_REAL_POPEN = subprocess.Popen


@pytest.mark.parametrize("pid,newer", [(0, False), (5150, False), (5150, True)])
def test_native_helper_only_clears_matching_positive_pid_attempt(service, monkeypatch, pid, newer):
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 4242)
    scripts = []
    monkeypatch.setattr(gateway.subprocess, "Popen", lambda cmd, **kw: scripts.append(cmd[-1]))
    gateway.refresh_launchd_plist_if_needed()
    assert service.marker.exists()
    if newer:
        service.marker.write_text("newer attempt")
    preamble = (
        "launchctl() { if [ \"$1\" = list ]; then printf '\"PID\" = " + str(pid) + ";\\n'; fi; }; "
        "sleep() { :; }; kill() { return 1; }; date() { printf '1'; }; "
        "grep() { local line; IFS= read -r line; [[ $line =~ ${@: -1} ]]; }; "
    )
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    result = _REAL_RUN(["/bin/bash", "-c", preamble + scripts[0]], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert service.marker.exists() is (pid == 0 or newer), result.stderr
    if newer:
        assert service.marker.read_text() == "newer attempt"



@pytest.mark.parametrize("entry", ["restart", "restart_all", "updater"])
@pytest.mark.parametrize("missing", [False, True])
def test_public_uninstall_cancels_failed_install_recovery(public_service, monkeypatch, entry, missing):
    from hermes_cli.update_cmd_fleet import _restart_launchd_gateway_after_update

    service = public_service
    if missing:
        service.plist.unlink()
    service.pid = 0
    with pytest.raises(SystemExit) as error:
        gateway.gateway_command(SimpleNamespace(gateway_command="install", force=True))
    assert error.value.code == 1
    assert service.marker.exists()
    assert service.plist.exists() is (not missing)
    gateway.gateway_command(SimpleNamespace(gateway_command="uninstall"))
    service.calls.clear()
    service.pid = 5150
    manual = []
    monkeypatch.setattr(gateway, "stop_profile_gateway", lambda: manual.append("stop") or False)
    monkeypatch.setattr(gateway, "kill_gateway_processes", lambda **kw: manual.append("kill-all") or 0)
    monkeypatch.setattr(gateway, "_wait_for_gateway_exit", lambda **kw: manual.append("wait") or True)
    monkeypatch.setattr(gateway, "run_gateway", lambda **kw: manual.append("run"))
    if entry == "updater":
        assert _restart_launchd_gateway_after_update() == ([], [])
        assert manual == []
    else:
        gateway.gateway_command(SimpleNamespace(gateway_command="restart", all=entry == "restart_all"))
        assert manual == ["kill-all" if entry == "restart_all" else "stop", "wait", "run"]
    assert not service.calls
    assert not service.plist.exists()
    assert not service.marker.exists()


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("point", ["unlink", "unreadable", "directory_fsync"])
def test_public_uninstall_cancellation_failure_is_explicit(public_service, monkeypatch, capsys, missing, point):
    service = public_service
    if missing:
        service.plist.unlink()
    service.marker.write_text("pending attempt")
    unlink = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path == service.marker:
            raise OSError("cancellation unlink failed")
        return unlink(path, *args, **kwargs)

    def fail_sync(path):
        raise OSError("cancellation directory sync failed")

    if point == "unlink":
        monkeypatch.setattr(Path, "unlink", fail_unlink)
    elif point == "unreadable":
        service.marker.chmod(0o666)
    else:
        monkeypatch.setattr(launchd_reload, "_launchd_fsync_directory", fail_sync)
    with pytest.raises(SystemExit) as error:
        gateway.gateway_command(SimpleNamespace(gateway_command="uninstall"))
    assert error.value.code == 1
    assert "Service uninstalled" not in capsys.readouterr().out
    assert service.marker.exists() is (point != "directory_fsync")


@pytest.mark.parametrize("entry", ["refresh_launchd_plist_if_needed", "launchd_install"])
@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_post_completion_output_failure_preserves_activated_image(service, monkeypatch, entry, missing, stream):
    import builtins

    if missing:
        service.plist.unlink()
    original_print = builtins.print
    unlink = Path.unlink
    fsync = os.fsync
    activated = []
    emitted = []

    def retire(path, *args, **kwargs):
        if path == service.marker:
            assert any(c[1] == "list" for c in service.calls)
            activated.append(launchd_reload._launchd_file_image(service.plist))
        return unlink(path, *args, **kwargs)

    def fail_sync(fd):
        if stream == "stderr" and activated and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("retirement directory sync failed")
        return fsync(fd)

    def fail_print(*args, **kwargs):
        text = " ".join(str(arg) for arg in args)
        target = kwargs.get("file", sys.stdout)
        if activated and target is getattr(sys, stream) and (
            stream == "stderr" or "to match the current Hermes install" in text or "installed and loaded" in text
        ):
            emitted.append(text)
            raise BrokenPipeError("closed diagnostic stream")
        return original_print(*args, **kwargs)

    monkeypatch.setattr(Path, "unlink", retire)
    monkeypatch.setattr(os, "fsync", fail_sync)
    monkeypatch.setattr(builtins, "print", fail_print)
    diagnostic_error = None
    try:
        if entry == "launchd_install":
            gateway.launchd_install(force=True)
        else:
            gateway.refresh_launchd_plist_if_needed()
    except (OSError, launchd_reload.LaunchdReloadError) as exc:
        diagnostic_error = exc
    assert emitted
    assert activated
    assert launchd_reload._launchd_file_image(service.plist) == activated[0]
    assert not service.marker.exists()
    assert not isinstance(diagnostic_error, launchd_reload.LaunchdReloadError)


def test_deferred_notice_failure_keeps_publication_and_pending(service, monkeypatch):
    import builtins

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 4242)
    submitted = []
    monkeypatch.setattr(gateway.subprocess, "Popen", lambda *a, **kw: submitted.append(True))
    original_print = builtins.print
    notices = []

    def fail_notice(*args, **kwargs):
        if "reload deferred" in " ".join(str(arg) for arg in args):
            notices.append(True)
            raise BrokenPipeError("deferred notice pipe closed")
        return original_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", fail_notice)
    diagnostic_error = None
    try:
        gateway.refresh_launchd_plist_if_needed()
    except (OSError, launchd_reload.LaunchdReloadError) as exc:
        diagnostic_error = exc
    assert submitted and notices
    assert service.plist.read_bytes() == b"new definition"
    assert service.marker.exists()
    assert not service.calls
    assert not isinstance(diagnostic_error, launchd_reload.LaunchdReloadError)


def test_retired_attempt_cannot_compensate_later_exception(service):
    activated = None
    with pytest.raises((OSError, launchd_reload.LaunchdReloadError)):
        with launchd_reload._launchd_plist_update(service.plist, "new definition") as attempt:
            assert gateway._launchctl_label_supervising_process(gateway.get_launchd_label())
            launchd_reload._clear_launchd_reload_pending(service.plist, attempt)
            activated = launchd_reload._launchd_file_image(service.plist)
            raise OSError("post-retirement cleanup failed")
    assert launchd_reload._launchd_file_image(service.plist) == activated
    assert not service.marker.exists()


def test_generated_helper_retirement_diagnostic_failure_is_cleanup_only(service, monkeypatch):
    import shlex

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 4242)
    monkeypatch.setattr(gateway, "get_python_path", lambda: "retire_python")
    scripts = []
    monkeypatch.setattr(gateway.subprocess, "Popen", lambda cmd, **kw: scripts.append(cmd[-1]))
    gateway.refresh_launchd_plist_if_needed()
    activated = launchd_reload._launchd_file_image(service.plist)
    child = (
        "import builtins, sys; import hermes_cli.gateway_launchd_reload as owner\n"
        "def fail_sync(path): raise OSError('retirement directory sync failed')\n"
        "def fail_print(*a, **kw): raise BrokenPipeError('closed helper stderr')\n"
        "owner._launchd_fsync_directory = fail_sync\n"
        "builtins.print = fail_print\n"
        "exec(sys.argv[1])\n"
    )
    preamble = (
        "launchctl() { if [ \"$1\" = list ]; then printf '\"PID\" = 5150;\\n'; fi; }; "
        "sleep() { :; }; kill() { return 1; }; date() { printf '1'; }; "
        "grep() { local line; IFS= read -r line; [[ $line =~ ${@: -1} ]]; }; "
        f"retire_python() {{ {shlex.join([sys.executable, '-B', '-c', child])} \"$2\"; }}; "
    )
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    result = _REAL_RUN(["/bin/bash", "-c", preamble + scripts[0]], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not service.marker.exists()
    assert launchd_reload._launchd_file_image(service.plist) == activated
    assert not result.stderr, result.stderr
