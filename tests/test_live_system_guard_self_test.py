"""Self-test for the live-system guard fixture in tests/conftest.py.

This file is the canary. If anyone removes a guard or weakens it, these
tests fail. If anyone adds a NEW kill primitive to the codebase without
adding it to the guard, the corresponding test added here will fail too.

The guard exists to protect the developer's live ``hermes-gateway`` process
from being SIGTERMed by tests. See PR #23397 for the original incident
(5+ live gateway kills in 3 days). Per Teknium 2026-05-10:

  > "You better do such a deep scan and scrub of the tests that this
  >  never is possible ever again for all eternity."

Every primitive that can deliver a signal to a foreign process, mutate the live
systemd unit, or enter the real launchd namespace MUST be exercised below.
Adding a new primitive to the guard? Add a test here too.
"""
from __future__ import annotations

import os
import signal
import subprocess
import types
from pathlib import Path

import pytest

# A guaranteed-foreign PID: PID 1 (init).  Owned by root, not us, and
# always exists. A sane guard refuses to signal it.
FOREIGN_PID = 1


# ──────────────────── fail-closed self-protection ──────────────
#
# This file executes REAL kill primitives — os.kill(-1, SIGTERM), os.killpg,
# pkill -f python — and depends entirely on the autouse ``_live_system_guard``
# fixture in tests/conftest.py to intercept them. That makes the canary
# fail-OPEN: in any collection context where this file is present but its home
# conftest is not, the primitives fire for real and ``os.kill(-1, SIGTERM)``
# SIGTERMs every process the invoking user owns (a full desktop-session kill was
# reported in the field — see issue #68311). Such contexts are not exotic:
# published sdists that ship ``tests/`` but not ``tests/conftest.py``, trees
# assembled by copying ``test*.py`` files (that glob does NOT match
# ``conftest.py``), ``pytest --noconftest``, or running from a foreign rootdir.
#
# The fixture below makes the canary fail-CLOSED instead: it refuses to run any
# test in this file unless the guard is provably active, so no collection
# context can ever detonate the primitives. The one thing the canary can detect
# about its own safety is that the guard monkeypatches ``os.kill`` with a plain
# Python function, whereas the unguarded primitive is a C builtin.


def _live_system_guard_is_active() -> bool:
    """True iff tests/conftest.py's ``_live_system_guard`` has patched os.kill.

    The guard replaces ``os.kill`` with a plain Python function; the raw,
    unguarded primitive is a C builtin (``types.BuiltinFunctionType``). If
    ``os.kill`` is still the builtin, the guard never loaded and every kill
    primitive in this file would fire for real.
    """
    return not isinstance(os.kill, types.BuiltinFunctionType)


@pytest.fixture(autouse=True)
def _refuse_to_fire_live_weapons(request):
    """Fail closed: refuse to run a canary test unless the guard is active.

    Tests genuinely marked ``@pytest.mark.live_system_guard_bypass`` opt out
    (they run the raw primitive deliberately and harmlessly, e.g. a signal-0
    liveness probe of our own PID), matching the guard's own bypass contract.
    """
    if request.node.get_closest_marker("live_system_guard_bypass"):
        yield
        return
    if not _live_system_guard_is_active():
        pytest.fail(
            "REFUSING TO RUN: the live-system guard from tests/conftest.py is "
            "not active in this interpreter (os.kill is still the raw C "
            "builtin). This canary file executes real kill primitives — "
            "os.kill(-1, SIGTERM), os.killpg, pkill -f python — and relies on "
            "the guard to intercept them; unguarded, they SIGTERM every process "
            "the current user owns. This usually means the file was collected "
            "without its home tests/conftest.py (note: a test*.py copy glob "
            "does NOT match conftest.py). See issue #68311.",
            pytrace=False,
        )
    yield


def test_fail_closed_probe_reports_guard_active():
    """In the real suite the guard is loaded, so the probe reports active and
    ``_refuse_to_fire_live_weapons`` stays out of the way (no false positives
    that would wedge CI)."""
    assert _live_system_guard_is_active() is True


def test_fail_closed_probe_classifies_raw_builtin_as_unguarded():
    """The probe's discriminator, exercised against real objects: a raw C
    builtin the guard never touches (``os.getpid``) is exactly what an
    unguarded ``os.kill`` looks like and must read as 'guard not active', while
    the loaded guard's ``os.kill`` is a plain Python function."""
    assert isinstance(os.getpid, types.BuiltinFunctionType)
    assert not isinstance(os.kill, types.BuiltinFunctionType)

def test_guard_home_does_not_claim_conventional_tmp_home(tmp_path):
    """Existing tests may create ``tmp_path / 'home'`` themselves."""
    assert Path.home().name == "_hermes_test_home"
    assert not (tmp_path / "home").exists()
    assert not any("live-system-guard-bin" in str(path) for path in tmp_path.rglob("*"))



# ──────────────────── kill primitives ─────────────────────────


def test_os_kill_blocks_foreign_pid():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.kill(FOREIGN_PID, signal.SIGTERM)


def test_os_kill_blocks_negative_one():
    """``os.kill(-1, sig)`` signals every process we can reach. Must be blocked."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.kill(-1, signal.SIGTERM)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="killpg POSIX-only")
def test_os_killpg_blocks_foreign_pgid():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.killpg(FOREIGN_PID, signal.SIGTERM)


# ──────────────────── subprocess regex bypasses ────────────────


def test_subprocess_run_systemctl_restart_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_run_full_path_systemctl_blocked():
    """``/usr/bin/systemctl`` (full path) must be blocked too."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["/usr/bin/systemctl", "--user", "stop", "hermes-gateway"])


def test_subprocess_run_sudo_systemctl_blocked():
    """``sudo systemctl ...`` defeated the old head==systemctl check."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["sudo", "systemctl", "restart", "hermes-gateway"])


def test_subprocess_run_env_systemctl_blocked():
    """``env systemctl ...`` similarly defeated the old head check."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["env", "systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_run_bash_c_systemctl_blocked():
    """``bash -c "systemctl ..."`` must also be caught."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["bash", "-c", "systemctl --user restart hermes-gateway"])


def test_subprocess_run_sh_c_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["sh", "-c", "systemctl --user stop hermes-gateway"])


def test_subprocess_run_setsid_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["setsid", "systemctl", "kill", "hermes-gateway"])


def test_subprocess_run_string_shell_true_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(
            "systemctl --user restart hermes-gateway",
            shell=True,
        )


@pytest.mark.parametrize(
    ("command", "kwargs"),
    [
        (["launchctl", "bootout", "gui/501/ai.hermes.gateway"], {}),
        (["/bin/launchctl", "bootout", "gui/501/ai.hermes.gateway"], {}),
        (["sudo", "launchctl", "bootout", "gui/501/ai.hermes.gateway"], {}),
        (["env", "launchctl", "bootout", "gui/501/ai.hermes.gateway"], {}),
        (["setsid", "launchctl", "bootout", "gui/501/ai.hermes.gateway"], {}),
        (["bash", "-c", "launchctl bootout gui/501/ai.hermes.gateway"], {}),
        ("bash -c 'launchctl bootout gui/501/ai.hermes.gateway'", {"shell": True}),
        ("bash -c 'echo $(launchctl print gui/501/ai.hermes.gateway)'", {"shell": True}),
        ("launchctl>/tmp/pytest-must-not-exist", {"shell": True}),
        (("launchctl", "bootout", "gui/501/ai.hermes.gateway"), {}),
        ([b"launchctl", b"bootout", b"gui/501/ai.hermes.gateway"], {}),
        (b"launchctl bootout gui/501/ai.hermes.gateway", {"shell": True}),
        (["ignored-argv-zero"], {"executable": "/bin/launchctl"}),
    ],
)
def test_subprocess_run_launchctl_shapes_are_blocked_before_exec(
    command, kwargs, monkeypatch
):
    """Every supported command shape must stop before the final executor."""
    escaped: list[object] = []

    def escaped_to_real_popen(*args, **popen_kwargs):
        escaped.append((args, popen_kwargs))
        raise AssertionError("launchctl escaped the live-system guard")

    # subprocess.run resolves subprocess.Popen at call time. Replacing that
    # final executor makes this regression safe even while RED: a missing
    # launchctl guard reaches only this trap, never the real operating system.
    monkeypatch.setattr(subprocess, "Popen", escaped_to_real_popen)

    with pytest.raises(RuntimeError, match="blocked.*launchctl"):
        subprocess.run(command, check=False, **kwargs)

    assert escaped == []


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("subprocess.Popen", ("/bin/launchctl", ["launchctl", "print"], None, None)),
        ("os.system", (b"launchctl print gui/501/ai.hermes.gateway",)),
        ("os.exec", ("/bin/launchctl", ["launchctl", "print"], None)),
        ("os.spawn", (0, "/bin/launchctl", ["launchctl", "print"], None)),
        ("os.posix_spawn", ("/bin/launchctl", ["launchctl", "print"], {})),
    ],
)
def test_process_global_audit_guard_blocks_launchctl_events(event, args, request):
    """Collection-time and direct os process primitives share the hard block."""
    matching_plugins = [
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if hasattr(plugin, "_pytest_launchctl_audit_guard")
    ]
    assert len(matching_plugins) == 1

    with pytest.raises(RuntimeError, match="blocked launchctl"):
        matching_plugins[0]._pytest_launchctl_audit_guard(event, args)


def test_launchctl_path_resolves_only_to_per_test_stub(
    tmp_path: Path, tmp_path_factory
):
    """Opaque child scripts can resolve only the harmless temporary stub."""
    import shutil

    resolved = Path(shutil.which("launchctl") or "")
    assert resolved.is_relative_to(tmp_path_factory.getbasetemp())
    assert not resolved.is_relative_to(tmp_path)
    assert resolved.name == "launchctl"
    assert "launchctl blocked" in resolved.read_text(encoding="utf-8")


def test_launchd_unit_tests_use_only_temporary_runtime_paths(tmp_path: Path) -> None:
    """Launchd tests must never resolve the operator's real runtime homes."""
    import hermes_cli.gateway as gateway_cli

    home = Path(os.environ["HOME"])
    hermes_home = Path(os.environ["HERMES_HOME"])
    launchd_home = gateway_cli._launchd_user_home()
    plist_path = gateway_cli.get_launchd_plist_path()

    assert home.is_relative_to(tmp_path)
    assert hermes_home.is_relative_to(tmp_path)
    assert launchd_home.is_relative_to(tmp_path)
    assert plist_path.is_relative_to(tmp_path)
    assert plist_path.parent == launchd_home / "Library" / "LaunchAgents"


def test_unmocked_launchd_uninstall_is_blocked_before_service_or_plist_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    """A forgotten subprocess mock must fail closed at the production caller."""
    import hermes_cli.gateway as gateway_cli

    escaped: list[object] = []

    def escaped_to_real_popen(*args, **kwargs):
        escaped.append((args, kwargs))
        raise AssertionError("launchd_uninstall escaped to real Popen")

    monkeypatch.setattr(subprocess, "Popen", escaped_to_real_popen)
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    old_bytes = b"installed plist must survive an unmocked test\n"
    plist_path.write_bytes(old_bytes)
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr(gateway_cli, "_launchd_domain", lambda: "gui/501")

    with pytest.raises(RuntimeError, match="blocked.*launchctl"):
        gateway_cli.launchd_uninstall()

    assert escaped == []
    assert plist_path.read_bytes() == old_bytes


def test_subprocess_popen_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(["systemctl", "--user", "stop", "hermes-gateway"])


def test_subprocess_call_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.call(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_check_call_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.check_call(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_check_output_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.check_output(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_getoutput_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.getoutput("systemctl --user restart hermes-gateway")


def test_subprocess_getstatusoutput_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.getstatusoutput("systemctl --user restart hermes-gateway")


# ──────────────────── os.system / os.popen ────────────────────


def test_os_system_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.system("systemctl --user restart hermes-gateway")


def test_os_popen_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.popen("systemctl --user restart hermes-gateway")


# ──────────────────── pty.spawn ────────────────────────────────


def test_pty_spawn_systemctl_blocked():
    import pty
    with pytest.raises(RuntimeError, match="live-system guard"):
        pty.spawn(["systemctl", "--user", "restart", "hermes-gateway"])


# ──────────────────── asyncio.create_subprocess_* ──────────────


def test_asyncio_create_subprocess_exec_systemctl_blocked():
    import asyncio

    async def _attempt():
        await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", "hermes-gateway"
        )

    with pytest.raises(RuntimeError, match="live-system guard"):
        asyncio.run(_attempt())


def test_asyncio_create_subprocess_shell_systemctl_blocked():
    import asyncio

    async def _attempt():
        await asyncio.create_subprocess_shell(
            "systemctl --user restart hermes-gateway"
        )

    with pytest.raises(RuntimeError, match="live-system guard"):
        asyncio.run(_attempt())


# ──────────────────── pkill / killall / taskkill ───────────────


def test_subprocess_pkill_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "hermes"])


def test_subprocess_pkill_hermes_gateway_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "hermes-gateway"])


def test_subprocess_pkill_python_dash_f_blocked():
    """``pkill -f python`` matches the gateway's "python -m hermes_cli.main"."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "python"])


def test_subprocess_killall_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["killall", "hermes"])


# ──────────────────── pass-through cases (must NOT raise) ──────
















# ──────────────────── bypass marker ─────────────────────────────


@pytest.mark.live_system_guard_bypass
def test_bypass_marker_disables_guard():
    """The bypass marker exists for tests that genuinely need real signal delivery
    (e.g. PTY tests SIGINTing their own child). Verify it works.

    We use it harmlessly here by signaling our own PID 0 (own group) so we
    don't actually kill anything — but the call goes through real os.kill.
    """
    # With bypass, os.kill remains real. Calling os.kill(os.getpid(), 0) just
    # checks that the PID exists — harmless.
    os.kill(os.getpid(), 0)  # No exception — signal guard is OFF.


@pytest.mark.live_system_guard_bypass
def test_bypass_marker_never_allows_launchctl(monkeypatch):
    """The signal-test bypass must never become a real-service bypass."""
    escaped: list[object] = []

    def escaped_to_real_popen(*args, **kwargs):
        escaped.append((args, kwargs))
        raise AssertionError("launchctl escaped through the bypass marker")

    monkeypatch.setattr(subprocess, "Popen", escaped_to_real_popen)

    with pytest.raises(RuntimeError, match="blocked.*launchctl"):
        subprocess.run(
            ["/bin/launchctl", "bootout", "gui/501/ai.hermes.gateway"],
            check=False,
        )

    assert escaped == []
