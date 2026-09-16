"""Launchd definition publication, caught-failure rollback, and pending activation.

Native public service APIs and actuation primitives remain in ``gateway``; late
imports here avoid a module-level cycle with that facade. Pending tokens support
retry, not power-loss rollback or atomic visibility across the two files.
"""

import contextlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


class LaunchdReloadError(RuntimeError):
    """A launchd definition refresh was refused or could not be activated."""


@dataclass(frozen=True)
class _LaunchdFileImage:
    data: bytes
    mode: int
    device: int
    inode: int


def _launchd_file_image(path: Path) -> _LaunchdFileImage | None:
    """Read only an owner-controlled regular file, without following a symlink."""
    import stat
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LaunchdReloadError(f"Cannot read launchd artifact {path}: {exc}") from exc
    try:
        metadata = os.fstat(fd)
        mode = stat.S_IMODE(metadata.st_mode)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or mode & 0o7022):
            raise LaunchdReloadError(f"launchd artifact is not a safe regular owner file: {path}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return _LaunchdFileImage(stream.read(), mode, metadata.st_dev, metadata.st_ino)
    finally:
        os.close(fd)


def _launchd_fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _launchd_staged_file(path: Path, data: bytes, mode: int):
    """Keep the scratch inode available until publication and durability checks finish."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
            metadata = os.fstat(stream.fileno())
        yield temporary, _LaunchdFileImage(data, mode, metadata.st_dev, metadata.st_ino)
    finally:
        temporary.unlink(missing_ok=True)


def _launchd_replace_image(path: Path, data: bytes, mode: int, expected: _LaunchdFileImage | None) -> None:
    with _launchd_staged_file(path, data, mode) as (temporary, _image):
        if _launchd_file_image(path) != expected:
            raise LaunchdReloadError(f"launchd artifact owner changed: {path}")
        os.replace(temporary, path)
        _launchd_fsync_directory(path.parent)


def _launchd_reload_pending_path(plist_path: Path) -> Path:
    return plist_path.with_name(plist_path.name + ".reload-pending")


def _launchd_reload_is_pending(plist_path: Path) -> bool:
    return _launchd_file_image(_launchd_reload_pending_path(plist_path)) is not None


def _clear_launchd_reload_pending(
    plist_path: Path, attempt: str, *, unsupported_marker: Path | None = None,
) -> str | None:
    """Retire an observed activation; return cleanup diagnostics for later output."""
    marker = _launchd_reload_pending_path(plist_path)
    image = _launchd_file_image(marker)
    if (image is None or image.data != attempt.encode("ascii")
            or _launchd_file_image(marker) != image):
        raise LaunchdReloadError("launchd retirement refused: pending attempt owner changed")
    if unsupported_marker is not None:
        # Keep pending authority if stale fallback evidence cannot be removed.
        unsupported_marker.unlink(missing_ok=True)
    marker.unlink()
    try:
        _launchd_fsync_directory(marker.parent)
    except OSError:
        # Positive activation and matching-token unlink already completed.
        return (
            "⚠ launchd activation succeeded; pending-marker directory sync failed. "
            "Cleanup durability is unconfirmed."
        )
    return None


def _cancel_launchd_reload_pending(plist_path: Path) -> None:
    """Uninstall revokes retry authority even when no definition remains."""
    marker = _launchd_reload_pending_path(plist_path)
    try:
        image = _launchd_file_image(marker)
        if image is None:
            return
        if _launchd_file_image(marker) != image:
            raise LaunchdReloadError("launchd cancellation refused: pending attempt owner changed")
        marker.unlink()
        _launchd_fsync_directory(marker.parent)
    except OSError as exc:
        raise LaunchdReloadError(f"Cannot cancel pending launchd recovery: {exc}") from exc


def _report_launchd_reload(message: str | None = None, *, warning: str | None = None) -> None:
    """Output cannot turn completed activation or deferred submission into failure."""
    if warning:
        with contextlib.suppress(OSError):
            print(warning, file=sys.stderr)
    if message:
        with contextlib.suppress(OSError):
            print(message)


@contextlib.contextmanager
def _launchd_actuation():
    """An actuation failure cannot prove that launchd did not load the definition."""
    try:
        yield
    except (OSError, subprocess.SubprocessError, LaunchdReloadError) as exc:
        # Preserve native exception types, including unsupported-domain fallback.
        exc._launchd_definition_may_be_loaded = True
        raise


@contextlib.contextmanager
def _launchd_plist_update(plist_path: Path, definition: str):
    """Bounded publication/rollback for this refresh, not a crash recovery journal.

    Only this pending attempt may compensate its published image, before direct
    launchd actuation starts. An actuation error leaves the new definition and
    pending token for retry because launchd may already have loaded those bytes.
    After observed activation and matching-token unlink, retirement directory-sync
    failure is cleanup only.
    """
    from hermes_cli import gateway

    previous = _launchd_file_image(plist_path)
    published = None
    attempt = os.urandom(16).hex()
    try:
        marker = _launchd_reload_pending_path(plist_path)
        _launchd_replace_image(marker, attempt.encode("ascii"), 0o600, _launchd_file_image(marker))
        mode = previous.mode if previous is not None else 0o600
        with _launchd_staged_file(plist_path, definition.encode("utf-8"), mode) as (temporary, image):
            if _launchd_file_image(plist_path) != previous:
                raise LaunchdReloadError("launchd publication refused: definition owner changed")
            os.replace(temporary, plist_path)
            published = image
            _launchd_fsync_directory(plist_path.parent)
        yield attempt
    except (OSError, subprocess.SubprocessError, LaunchdReloadError) as exc:
        if published is not None and not getattr(exc, "_launchd_definition_may_be_loaded", False):
            try:
                pending = _launchd_file_image(marker)
                if pending is None or pending.data != attempt.encode("ascii"):
                    raise LaunchdReloadError("launchd rollback refused: pending attempt owner changed")
                if _launchd_file_image(plist_path) != published:
                    raise LaunchdReloadError("launchd rollback refused: definition owner changed")
                if previous is None:
                    plist_path.unlink()
                    _launchd_fsync_directory(plist_path.parent)
                else:
                    _launchd_replace_image(plist_path, previous.data, previous.mode, published)
            except (OSError, LaunchdReloadError) as rollback:
                raise LaunchdReloadError(f"{exc}; rollback failed: {rollback}") from exc
        if isinstance(exc, LaunchdReloadError):
            raise
        # Preserve terminal native fallback errors after the EIO recovery attempt.
        if isinstance(exc, subprocess.CalledProcessError) and gateway._launchctl_domain_unsupported(exc.returncode):
            raise
        raise LaunchdReloadError(f"launchd update failed: {exc}") from exc


def _reload_launchd_plist(plist_path: Path, attempt: str, *, check_bootstrap: bool) -> tuple[bool, str | None]:
    """Activate the definition; return (deferred, cleanup warning) without reporting."""
    from hermes_cli import gateway

    label = gateway.get_launchd_label()
    domain = gateway._launchd_domain()
    target = f"{domain}/{label}"

    # Inside the gateway's launchd process tree (agent self-update) a direct bootout kills THIS CLI
    # before bootstrap runs, leaving the job unloaded with no KeepAlive.
    try:
        from gateway.status import get_running_pid
        gateway_pid = get_running_pid()
    except Exception:
        gateway_pid = None

    # POSIX ancestry is NOT a reliable "bootout will kill us" test (coalition membership survives
    # reparenting), so a running gateway requires the independent native helper.
    if (
        gateway_pid is not None
        and hasattr(os, "setsid")  # POSIX-only; launchd is macOS so always true here
    ) and gateway._spawn_deferred_launchd_reload(
        domain=domain, label=label, target=target, plist_path=plist_path, gateway_pid=gateway_pid,
        attempt=attempt,
    ):
        return True, None

    with _launchd_actuation():
        # Bootout/bootstrap so launchd reads the new definition; bootstrap can fail silently under load
        # during a drain, and KeepAlive can't revive an unregistered job.
        # Captured: best-effort (the job may already be unloaded), keep expected noise off the terminal.
        subprocess.run(["launchctl", "bootout", target], check=False, timeout=90, **gateway._CAPTURE_TEXT)
        _reload_budget = gateway._launchd_reload_budget()
        # Wait out the old gateway's drain first so the budget isn't burned on guaranteed EIO ("already loaded").
        if gateway_pid is not None and not gateway._wait_for_pid_exit(gateway_pid, _reload_budget):
            gateway._append_launchd_reload_log(
                f"old gateway pid {gateway_pid} still alive after "
                f"{int(_reload_budget)}s drain wait — bootstrapping {target} anyway"
            )
        _deadline = time.monotonic() + _reload_budget
        last_error = []
        bootstrap_succeeded = gateway._retry_launchctl_bootstrap_until_registered(
            domain, plist_path, label, deadline=_deadline, last_error=last_error
        )
        if not bootstrap_succeeded:
            gateway._append_launchd_reload_log(
                f"FAILED launchd reload of {target} — service NOT registered after "
                f"retrying for {int(_reload_budget)}s (in-process fallback path)"
            )
            gateway.logger.error(
                "launchd reload of %s failed — service not registered after %ds of retries; see %s",
                target, int(_reload_budget), gateway._launchd_reload_log_path(),
            )
            print(
                "✗ Updated the launchd plist but the service did not re-register; "
                f"see {gateway._launchd_reload_log_path()}"
            )
            if (check_bootstrap and last_error
                    and gateway._launchctl_domain_unsupported(last_error[-1].returncode)):
                raise last_error[-1]
            raise LaunchdReloadError(f"launchd reload failed for {target}; pending marker preserved")
        warning = _clear_launchd_reload_pending(plist_path, attempt)
        return False, warning
