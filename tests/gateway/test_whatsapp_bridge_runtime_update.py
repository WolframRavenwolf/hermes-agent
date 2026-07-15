"""Transactional update coverage for the persistent WhatsApp bridge runtime."""

from __future__ import annotations

import errno
import json
import logging
import multiprocessing
import os
import queue
import stat
import sys
import sysconfig
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from typing import cast

import pytest

from gateway.platforms import whatsapp_common
from plugins.platforms.whatsapp import adapter as whatsapp_adapter


_RUNTIME_SOURCES = {
    "bridge.js": "// bridge v{version}\n",
    "bridge_helpers.js": "// helpers v{version}\n",
    "allowlist.js": "// allowlist v{version}\n",
    "outbound_ids.js": "// outbound ids v{version}\n",
    "owner_message_gate.js": "// owner gate v{version}\n",
}
_RUNTIME_FILES = (*_RUNTIME_SOURCES, "package.json", "package-lock.json")


def _seed_runtime(root: Path, version: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, template in _RUNTIME_SOURCES.items():
        (root / name).write_text(template.format(version=version), encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "hermes-whatsapp-bridge",
                "version": version,
                "hermesRuntimeFiles": list(_RUNTIME_FILES),
                "dependencies": {"example": version},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "hermes-whatsapp-bridge",
                "version": version,
                "lockfileVersion": 3,
                "packages": {"": {"version": version}},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _successful_npm_ci(calls: list[tuple[list[str], Path]]):
    def run(command, *, cwd, **kwargs):
        stage = Path(cwd)
        calls.append((list(command), stage))
        assert not (stage / "node_modules").exists()
        modules = stage / "node_modules"
        modules.mkdir()
        (modules / "installed-version").write_text("new\n", encoding="utf-8")
        return CompletedProcess(command, 0, stdout="", stderr="")

    return run


def _staging_leftovers(target: Path) -> list[Path]:
    return sorted(target.parent.glob(f".{target.name}.*-*"))


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _multiprocess_resolver_worker(
    bundled: str,
    hermes_home: str,
    install_release,
    npm_entries,
    results,
    start_barrier=None,
) -> None:
    """Spawn-safe resolver worker with a deterministic, offline npm stand-in."""
    os.environ["HERMES_HOME"] = hermes_home

    def offline_npm_ci(command, *, cwd, **kwargs):
        npm_entries.put(os.getpid())
        if not install_release.wait(timeout=15):
            raise TimeoutError("test did not release simulated npm ci")
        modules = Path(cwd) / "node_modules"
        modules.mkdir()
        (modules / "installed-version").write_text("complete\n", encoding="utf-8")
        return CompletedProcess(command, 0, stdout="", stderr="")

    try:
        whatsapp_common.subprocess.run = offline_npm_ci
        whatsapp_common.find_node_executable = lambda name: "npm"
        if start_barrier is not None:
            start_barrier.wait(timeout=15)
        resolved = whatsapp_common.resolve_whatsapp_bridge_dir(
            bundled_bridge=Path(bundled),
            install_writable=False,
        )
        results.put(("ok", str(resolved)))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def test_dependency_fingerprint_covers_package_and_lock(tmp_path):
    bridge_dir = tmp_path / "bridge"
    _seed_runtime(bridge_dir, "1.0.0")

    original = whatsapp_common.whatsapp_bridge_dependency_fingerprint(bridge_dir)
    assert original

    (bridge_dir / "package.json").write_text("{}\n", encoding="utf-8")
    package_changed = whatsapp_common.whatsapp_bridge_dependency_fingerprint(bridge_dir)
    assert package_changed and package_changed != original

    _seed_runtime(bridge_dir, "1.0.0")
    (bridge_dir / "package-lock.json").write_text("{}\n", encoding="utf-8")
    assert whatsapp_common.whatsapp_bridge_dependency_fingerprint(bridge_dir) not in {
        "",
        original,
    }


def test_framed_hashes_distinguish_ambiguous_runtime_and_manifest_boundaries(
    tmp_path,
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _seed_runtime(first, "1.0.0")
    _seed_runtime(second, "1.0.0")
    (first / "bridge.js").write_bytes(b"ab")
    (first / "bridge_helpers.js").write_bytes(b"c")
    (second / "bridge.js").write_bytes(b"a")
    (second / "bridge_helpers.js").write_bytes(b"bc")

    assert (
        (first / "bridge.js").read_bytes()
        + (first / "bridge_helpers.js").read_bytes()
        == (second / "bridge.js").read_bytes()
        + (second / "bridge_helpers.js").read_bytes()
    )
    assert whatsapp_common.whatsapp_bridge_source_hash(
        first / "bridge.js"
    ) != whatsapp_common.whatsapp_bridge_source_hash(second / "bridge.js")

    (first / "package.json").write_bytes(b"ab")
    (first / "package-lock.json").write_bytes(b"c")
    (second / "package.json").write_bytes(b"a")
    (second / "package-lock.json").write_bytes(b"bc")
    assert whatsapp_common.whatsapp_bridge_dependency_fingerprint(
        first
    ) != whatsapp_common.whatsapp_bridge_dependency_fingerprint(second)


def test_lock_identity_canonicalizes_symlink_aliases_and_uses_private_root(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / "home"
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(physical, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_home)

    physical_lock = whatsapp_common._whatsapp_bridge_transaction_lock_path(
        physical / "bridge"
    )
    alias_lock = whatsapp_common._whatsapp_bridge_transaction_lock_path(
        alias / "bridge"
    )
    assert alias_lock == physical_lock

    with whatsapp_common._exclusive_whatsapp_bridge_transaction(alias / "bridge"):
        assert physical_lock.is_file()
    assert physical_lock.parent == hermes_home / ".whatsapp-bridge-locks"
    if os.name != "nt":
        assert physical_lock.parent.stat().st_mode & 0o777 == 0o700
        assert physical_lock.stat().st_mode & 0o777 == 0o600


def test_secure_lock_open_rejects_preplanted_symlink(tmp_path, monkeypatch):
    hermes_home = tmp_path / "home"
    bridge_dir = tmp_path / "bridge"
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_home)
    lock_path = whatsapp_common._whatsapp_bridge_transaction_lock_path(bridge_dir)
    whatsapp_common._validate_private_lock_root(lock_path.parent)
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    try:
        lock_path.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(
        whatsapp_common.WhatsAppBridgeDependencyError,
        match="symlink or reparse point",
    ):
        with whatsapp_common._exclusive_whatsapp_bridge_transaction(bridge_dir):
            pass
    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_simulated_windows_lock_acquire_and_release(monkeypatch):
    calls = []

    class FakeMsvcrt:
        LK_NBLCK = 2
        LK_UNLCK = 0

        @staticmethod
        def locking(descriptor, operation, length):
            calls.append((descriptor, operation, length))

    monkeypatch.setattr(whatsapp_common, "_msvcrt", FakeMsvcrt)
    monkeypatch.setattr(whatsapp_common, "_uses_windows_file_locking", lambda: True)
    with tempfile.TemporaryFile() as lock_file:
        whatsapp_common._try_acquire_whatsapp_bridge_file_lock(lock_file)
        whatsapp_common._release_whatsapp_bridge_file_lock(lock_file)
        assert calls == [
            (lock_file.fileno(), FakeMsvcrt.LK_NBLCK, 1),
            (lock_file.fileno(), FakeMsvcrt.LK_UNLCK, 1),
        ]


def test_windows_reparse_metadata_is_rejected_for_lock_files(tmp_path):
    lock_file = tmp_path / "lock"
    lock_file.write_bytes(b"\0")
    real_metadata = lock_file.stat()

    class ReparseMetadata:
        st_mode = real_metadata.st_mode
        st_dev = real_metadata.st_dev
        st_ino = real_metadata.st_ino
        st_uid = real_metadata.st_uid
        st_file_attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    reparse_metadata = cast(os.stat_result, ReparseMetadata())
    assert whatsapp_common._is_windows_reparse_point(reparse_metadata)
    with pytest.raises(OSError, match="opened regular file"):
        whatsapp_common._validate_lock_file_metadata(
            reparse_metadata, reparse_metadata
        )


def test_shared_dependency_installer_is_deterministic_and_transactional(
    tmp_path, monkeypatch
):
    bridge_dir = tmp_path / "bridge"
    _seed_runtime(bridge_dir, "1.0.0")
    old_modules = bridge_dir / "node_modules"
    old_modules.mkdir()
    (old_modules / "old-working-dependency").write_text("keep\n", encoding="utf-8")
    (old_modules / ".hermes-pkg-hash").write_text("stale", encoding="utf-8")
    calls = []

    def successful_ci(command, *, cwd, **kwargs):
        stage = Path(cwd)
        calls.append((list(command), stage))
        assert stage != bridge_dir
        (stage / "node_modules").mkdir()
        (stage / "node_modules" / "new-dependency").write_text(
            "installed\n", encoding="utf-8"
        )
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(whatsapp_common.subprocess, "run", successful_ci)

    assert whatsapp_common.ensure_whatsapp_bridge_dependencies(bridge_dir) is True

    assert calls[0][0][-2:] == ["ci", "--silent"]
    assert not (old_modules / "old-working-dependency").exists()
    assert (old_modules / "new-dependency").exists()
    assert (old_modules / ".hermes-pkg-hash").read_text(
        encoding="utf-8"
    ) == whatsapp_common.whatsapp_bridge_dependency_fingerprint(bridge_dir)
    assert _staging_leftovers(bridge_dir) == []


def test_failed_shared_dependency_install_keeps_old_tree_and_stamp(
    tmp_path, monkeypatch
):
    bridge_dir = tmp_path / "bridge"
    _seed_runtime(bridge_dir, "1.0.0")
    modules = bridge_dir / "node_modules"
    modules.mkdir()
    (modules / "old-working-dependency").write_text("keep\n", encoding="utf-8")
    stamp = modules / ".hermes-pkg-hash"
    stamp.write_text("old-stamp", encoding="utf-8")
    before = _file_snapshot(modules)
    monkeypatch.setattr(
        whatsapp_common.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(
            command, 1, stdout="", stderr="offline"
        ),
    )

    with pytest.raises(
        whatsapp_common.WhatsAppBridgeDependencyError, match="offline"
    ):
        whatsapp_common.ensure_whatsapp_bridge_dependencies(bridge_dir)

    assert (modules / "old-working-dependency").exists()
    assert stamp.read_text(encoding="utf-8") == "old-stamp"
    assert _file_snapshot(modules) == before
    assert _staging_leftovers(bridge_dir) == []


def test_npm_unavailable_is_typed_and_never_falls_back_to_bare_path(
    tmp_path, monkeypatch
):
    bridge_dir = tmp_path / "bridge"
    _seed_runtime(bridge_dir, "1.0.0")
    modules = bridge_dir / "node_modules"
    modules.mkdir()
    (modules / "old-working-dependency").write_text("keep\n", encoding="utf-8")
    before = _file_snapshot(modules)
    monkeypatch.setattr(whatsapp_common, "find_node_executable", lambda name: None)
    monkeypatch.setattr(
        whatsapp_common.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("bare npm must never be attempted")
        ),
    )

    with pytest.raises(
        whatsapp_common.WhatsAppBridgeUnavailableError,
        match="npm is unavailable",
    ):
        whatsapp_common.ensure_whatsapp_bridge_dependencies(bridge_dir)

    assert _file_snapshot(modules) == before


def test_npm_receives_minimal_environment_without_provider_or_messaging_secrets(
    tmp_path, monkeypatch
):
    bridge_dir = tmp_path / "bridge"
    _seed_runtime(bridge_dir, "1.0.0")
    captured = {}
    monkeypatch.setattr(whatsapp_common, "find_node_executable", lambda name: "/npm")
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("NPM_CONFIG_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "messaging-secret")
    monkeypatch.setenv("NPM_TOKEN", "registry-secret")

    def successful_ci(command, *, cwd, env, **kwargs):
        captured.update(env)
        (Path(cwd) / "node_modules").mkdir()
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(whatsapp_common.subprocess, "run", successful_ci)
    assert whatsapp_common.ensure_whatsapp_bridge_dependencies(bridge_dir) is True

    assert captured["HOME"] == str(tmp_path / "home")
    assert captured["TMPDIR"] == str(tmp_path / "tmp")
    assert captured["NPM_CONFIG_CACHE"] == str(tmp_path / "cache")
    assert all("secret" not in value for value in captured.values())
    assert "OPENAI_API_KEY" not in captured
    assert "WHATSAPP_ACCESS_TOKEN" not in captured
    assert "NPM_TOKEN" not in captured


def test_dependency_error_is_bounded_and_redacts_registry_credentials(
    tmp_path, monkeypatch
):
    bridge_dir = tmp_path / "bridge"
    _seed_runtime(bridge_dir, "1.0.0")
    secret = "npm_abcdefghijklmnopqrstuvwxyz"
    registry_url = f"https://user:{secret}@registry.example.invalid/pkg"
    noisy_error = "\n".join(["diagnostic"] * 30 + [registry_url])
    monkeypatch.setattr(
        whatsapp_common.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(
            command, 1, stdout="", stderr=noisy_error
        ),
    )

    with pytest.raises(whatsapp_common.WhatsAppBridgeDependencyError) as error:
        whatsapp_common.ensure_whatsapp_bridge_dependencies(bridge_dir)

    detail = str(error.value)
    assert secret not in detail
    assert registry_url not in detail
    assert "<redacted-url>" in detail
    assert len(detail) < 1400


def test_dependency_promotion_failure_rolls_back_exact_old_tree(
    tmp_path, monkeypatch
):
    bridge_dir = tmp_path / "bridge"
    _seed_runtime(bridge_dir, "1.0.0")
    modules = bridge_dir / "node_modules"
    modules.mkdir()
    (modules / "old-working-dependency").write_text("keep\n", encoding="utf-8")
    (modules / ".hermes-pkg-hash").write_text("old-stamp", encoding="utf-8")
    before = _file_snapshot(modules)
    calls = []
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )
    real_replace = os.replace

    def fail_dependency_promotion(source, destination):
        source_path = Path(source)
        if source_path.name == "node_modules" and Path(destination) == modules:
            raise OSError("simulated dependency promotion failure")
        return real_replace(source, destination)

    monkeypatch.setattr(whatsapp_common.os, "replace", fail_dependency_promotion)

    with pytest.raises(whatsapp_common.WhatsAppBridgeDependencyError):
        whatsapp_common.ensure_whatsapp_bridge_dependencies(bridge_dir)

    assert _file_snapshot(modules) == before
    assert not list(bridge_dir.glob(".node_modules.staging-*"))
    assert not list(bridge_dir.glob(".node_modules.backup-*"))


def test_dependency_restore_failure_keeps_recovery_and_both_diagnostics(
    tmp_path, monkeypatch
):
    bridge_dir = tmp_path / "bridge"
    _seed_runtime(bridge_dir, "1.0.0")
    modules = bridge_dir / "node_modules"
    modules.mkdir()
    (modules / "old-working-dependency").write_text("keep\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )
    real_replace = os.replace

    def fail_promotion_and_restore(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name == "node_modules" and destination_path == modules:
            raise OSError("simulated dependency promotion failure")
        if source_path.name.startswith(".node_modules.backup-"):
            raise OSError("simulated dependency restore failure")
        return real_replace(source, destination)

    monkeypatch.setattr(
        whatsapp_common.os, "replace", fail_promotion_and_restore
    )

    with pytest.raises(
        whatsapp_common.WhatsAppBridgeDependencyError,
        match="Rollback also failed",
    ) as error:
        whatsapp_common.ensure_whatsapp_bridge_dependencies(bridge_dir)

    detail = str(error.value)
    assert "simulated dependency promotion failure" in detail
    assert "simulated dependency restore failure" in detail
    assert "Recovery data was preserved" in detail
    backups = list(bridge_dir.glob(".node_modules.backup-*"))
    staging = list(bridge_dir.glob(".node_modules.staging-*"))
    assert len(backups) == 1
    assert len(staging) == 1
    assert (backups[0] / "old-working-dependency").exists()


def test_post_promotion_manifest_mismatch_rolls_dependencies_back(
    tmp_path, monkeypatch
):
    bridge_dir = tmp_path / "bridge"
    _seed_runtime(bridge_dir, "1.0.0")
    modules = bridge_dir / "node_modules"
    modules.mkdir()
    (modules / "old-working-dependency").write_text("keep\n", encoding="utf-8")
    (modules / ".hermes-pkg-hash").write_text("old-stamp", encoding="utf-8")
    before = _file_snapshot(modules)
    calls = []
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )
    real_replace = os.replace

    def tamper_after_dependency_promotion(source, destination):
        result = real_replace(source, destination)
        if Path(source).name == "node_modules" and Path(destination) == modules:
            (bridge_dir / "package-lock.json").write_text(
                '{"tampered": true}\n', encoding="utf-8"
            )
        return result

    monkeypatch.setattr(
        whatsapp_common.os, "replace", tamper_after_dependency_promotion
    )

    with pytest.raises(
        whatsapp_common.WhatsAppBridgeDependencyError,
        match="changed after promotion",
    ):
        whatsapp_common.ensure_whatsapp_bridge_dependencies(bridge_dir)

    assert _file_snapshot(modules) == before
    assert not list(bridge_dir.glob(".node_modules.backup-*"))


def test_lock_timeout_fails_closed_before_dependency_freshness_or_install(
    tmp_path, monkeypatch
):
    bridge_dir = tmp_path / "bridge"
    _seed_runtime(bridge_dir, "1.0.0")
    modules = bridge_dir / "node_modules"
    modules.mkdir()
    (modules / "old-working-dependency").write_text("keep\n", encoding="utf-8")
    (modules / ".hermes-pkg-hash").write_text("old-stamp", encoding="utf-8")
    before = _file_snapshot(modules)

    def always_busy(lock_file):
        raise BlockingIOError(errno.EAGAIN, "held by another process")

    monkeypatch.setattr(
        whatsapp_common, "_try_acquire_whatsapp_bridge_file_lock", always_busy
    )
    monkeypatch.setattr(whatsapp_common, "_WHATSAPP_BRIDGE_LOCK_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(
        whatsapp_common.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("npm must not run without the transaction lock")
        ),
    )

    with pytest.raises(
        whatsapp_common.WhatsAppBridgeBusyError,
        match="Timed out waiting for another Hermes process",
    ):
        whatsapp_common.ensure_whatsapp_bridge_dependencies(bridge_dir)

    assert _file_snapshot(modules) == before
    lock_path = whatsapp_common._whatsapp_bridge_transaction_lock_path(bridge_dir)
    assert bridge_dir not in lock_path.parents
    assert lock_path.parent != bridge_dir


def test_precommit_staging_cleanup_failure_preserves_primary_error_and_old_stamp(
    tmp_path, monkeypatch, caplog
):
    bridge_dir = tmp_path / "bridge"
    _seed_runtime(bridge_dir, "1.0.0")
    modules = bridge_dir / "node_modules"
    modules.mkdir()
    (modules / "old-working-dependency").write_text("keep\n", encoding="utf-8")
    (modules / ".hermes-pkg-hash").write_text("old-stamp", encoding="utf-8")
    before = _file_snapshot(modules)
    monkeypatch.setattr(
        whatsapp_common.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(
            command, 1, stdout="", stderr="primary npm failure"
        ),
    )
    real_remove = whatsapp_common._remove_path_without_following
    secret = "npm_cleanup_secret"

    def fail_staging_cleanup(path):
        if Path(path).name.startswith(".node_modules.staging-"):
            raise OSError(
                f"cleanup failed at https://user:{secret}@registry.invalid/stage"
            )
        return real_remove(path)

    monkeypatch.setattr(
        whatsapp_common, "_remove_path_without_following", fail_staging_cleanup
    )
    caplog.set_level(logging.WARNING, logger=whatsapp_common.logger.name)

    with pytest.raises(
        whatsapp_common.WhatsAppBridgeDependencyError, match="primary npm failure"
    ):
        whatsapp_common.ensure_whatsapp_bridge_dependencies(bridge_dir)

    assert _file_snapshot(modules) == before
    assert secret not in caplog.text
    assert "<redacted-url>" in caplog.text
    assert "after failure" in caplog.text


def test_postcommit_cleanup_failures_are_redacted_and_do_not_replace_success(
    tmp_path, monkeypatch, caplog
):
    bridge_dir = tmp_path / "bridge"
    _seed_runtime(bridge_dir, "1.0.0")
    modules = bridge_dir / "node_modules"
    modules.mkdir()
    (modules / "old-working-dependency").write_text("keep\n", encoding="utf-8")
    (modules / ".hermes-pkg-hash").write_text("old-stamp", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )
    real_remove = whatsapp_common._remove_path_without_following
    secret = "npm_cleanup_secret"

    def fail_postcommit_cleanup(path):
        name = Path(path).name
        if name.startswith((".node_modules.backup-", ".node_modules.staging-")):
            raise OSError(
                f"cleanup failed at https://user:{secret}@registry.invalid/postcommit"
            )
        return real_remove(path)

    monkeypatch.setattr(
        whatsapp_common, "_remove_path_without_following", fail_postcommit_cleanup
    )
    caplog.set_level(logging.WARNING, logger=whatsapp_common.logger.name)

    assert whatsapp_common.ensure_whatsapp_bridge_dependencies(bridge_dir) is True

    assert not (modules / "old-working-dependency").exists()
    assert (modules / "installed-version").read_text(encoding="utf-8") == "new\n"
    assert (modules / ".hermes-pkg-hash").read_text(
        encoding="utf-8"
    ) == whatsapp_common.whatsapp_bridge_dependency_fingerprint(bridge_dir)
    assert secret not in caplog.text
    assert "<redacted-url>" in caplog.text
    assert "after activation" in caplog.text


def test_stale_persistent_runtime_is_built_in_staging_then_replaced(
    tmp_path, monkeypatch
):
    bundled = tmp_path / "readonly-install" / "whatsapp-bridge"
    persistent = tmp_path / "hermes-home" / "scripts" / "whatsapp-bridge"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    (persistent / "node_modules" / "installed-version").write_text(
        "old\n", encoding="utf-8"
    )
    # Auth/session state is user data, not part of the replaceable runtime.
    (persistent / "session" / "nested").mkdir(parents=True)
    (persistent / "session" / "nested" / "creds.json").write_text(
        "secret-state\n", encoding="utf-8"
    )

    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(whatsapp_common, "find_node_executable", lambda name: "/managed/npm")
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )

    resolved = whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=bundled,
        persistent_bridge=persistent,
        install_writable=False,
    )

    assert resolved == persistent
    assert len(calls) == 1
    command, staging = calls[0]
    assert command == ["/managed/npm", "ci", "--silent"]
    assert staging != persistent
    assert staging.parent.parent == persistent.parent
    for name in (*_RUNTIME_SOURCES, "package.json", "package-lock.json"):
        assert (persistent / name).read_bytes() == (bundled / name).read_bytes()
    assert (persistent / "node_modules" / "installed-version").read_text(
        encoding="utf-8"
    ) == "new\n"
    assert (persistent / "session" / "nested" / "creds.json").read_text(
        encoding="utf-8"
    ) == "secret-state\n"
    assert (persistent / "node_modules" / ".hermes-pkg-hash").read_text(
        encoding="utf-8"
    ) == whatsapp_common.whatsapp_bridge_dependency_fingerprint(persistent)
    assert _staging_leftovers(persistent) == []


def test_persistent_state_symlink_is_preserved_without_dereferencing(
    tmp_path, monkeypatch
):
    bundled = tmp_path / "install"
    persistent = tmp_path / "persistent"
    external_state = tmp_path / "external-creds.json"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    external_state.write_text("secret-state\n", encoding="utf-8")
    state_link = persistent / "custom-state-link"
    try:
        state_link.symlink_to(external_state)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )

    assert whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=bundled,
        persistent_bridge=persistent,
        install_writable=False,
    ) == persistent

    promoted_link = persistent / "custom-state-link"
    assert promoted_link.is_symlink()
    assert os.readlink(promoted_link) == str(external_state)
    assert external_state.read_text(encoding="utf-8") == "secret-state\n"


def test_persistent_root_symlink_is_rejected_fail_closed(tmp_path, monkeypatch):
    bundled = tmp_path / "install"
    real_persistent = tmp_path / "real-persistent"
    persistent_alias = tmp_path / "persistent-alias"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(real_persistent, "1.0.0")
    try:
        persistent_alias.symlink_to(real_persistent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path / "home")

    with pytest.raises(
        whatsapp_common.WhatsAppBridgeStateError,
        match="persistent WhatsApp bridge root must be a real directory",
    ):
        whatsapp_common.resolve_whatsapp_bridge_dir(
            bundled_bridge=bundled,
            persistent_bridge=persistent_alias,
            install_writable=False,
        )


def test_state_copier_rejects_simulated_interior_windows_reparse_point(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    interior = source / "junction"
    interior.mkdir(parents=True)
    (interior / "secret").write_text("do not traverse", encoding="utf-8")
    interior_inode = interior.lstat().st_ino
    real_is_reparse = whatsapp_common._is_windows_reparse_point
    monkeypatch.setattr(
        whatsapp_common,
        "_is_windows_reparse_point",
        lambda metadata: metadata.st_ino == interior_inode or real_is_reparse(metadata),
    )

    with pytest.raises(
        whatsapp_common.WhatsAppBridgeStateError,
        match="interior junction or reparse point",
    ):
        whatsapp_common._copy_persistent_bridge_state(source, destination)


def test_final_backup_merge_never_overwrites_newer_live_state(
    tmp_path, monkeypatch
):
    bundled = tmp_path / "install"
    persistent = tmp_path / "persistent"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    (persistent / "session").mkdir()
    state_file = persistent / "session" / "creds.json"
    state_file.write_text("old-backup-state\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )
    real_copy = whatsapp_common._copy_persistent_bridge_state

    def inject_live_write(source, destination, *, overwrite=True):
        if Path(source).name.startswith(".persistent.backup-"):
            live_state = Path(destination) / "session" / "creds.json"
            live_state.write_text("newer-live-state\n", encoding="utf-8")
        return real_copy(source, destination, overwrite=overwrite)

    monkeypatch.setattr(
        whatsapp_common, "_copy_persistent_bridge_state", inject_live_write
    )

    assert whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=bundled,
        persistent_bridge=persistent,
        install_writable=False,
    ) == persistent
    assert state_file.read_text(encoding="utf-8") == "newer-live-state\n"


@pytest.mark.parametrize(
    ("changed_file", "replacement"),
    [
        ("bridge.js", "// changed bridge\n"),
        ("bridge_helpers.js", "// changed helper\n"),
        ("allowlist.js", "// changed imported source\n"),
        (
            "package.json",
            json.dumps(
                {
                    "name": "hermes-whatsapp-bridge",
                    "version": "9.9.9",
                    "hermesRuntimeFiles": list(_RUNTIME_FILES),
                    "dependencies": {},
                },
                sort_keys=True,
            )
            + "\n",
        ),
        (
            "package-lock.json",
            '{"name":"hermes-whatsapp-bridge","version":"1.0.0","lockfileVersion":3,"packages":{"":{"version":"9.9.9"}}}\n',
        ),
    ],
)
def test_each_runtime_source_or_manifest_change_triggers_refresh(
    tmp_path, monkeypatch, changed_file, replacement
):
    bundled = tmp_path / "install"
    persistent = tmp_path / "persistent"
    _seed_runtime(bundled, "1.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    (bundled / changed_file).write_text(replacement, encoding="utf-8")

    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(whatsapp_common, "find_node_executable", lambda name: "npm")
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )

    assert whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=bundled,
        persistent_bridge=persistent,
        install_writable=False,
    ) == persistent

    assert len(calls) == 1
    assert (persistent / changed_file).read_text(encoding="utf-8") == replacement


def test_current_persistent_runtime_is_reused_without_npm(tmp_path, monkeypatch):
    bundled = tmp_path / "install"
    persistent = tmp_path / "persistent"
    _seed_runtime(bundled, "1.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    (persistent / "node_modules" / "sentinel").write_text("keep\n", encoding="utf-8")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("npm must not run for an identical persistent runtime")

    monkeypatch.setattr(whatsapp_common.subprocess, "run", unexpected_run)

    assert whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=bundled,
        persistent_bridge=persistent,
        install_writable=False,
    ) == persistent
    assert (persistent / "node_modules" / "sentinel").exists()


def test_multiprocess_resolvers_do_not_interleave_and_leave_complete_runtime_and_state(
    tmp_path,
):
    bundled = tmp_path / "readonly-install" / "whatsapp-bridge"
    hermes_home = tmp_path / "shared-hermes-home"
    persistent = hermes_home / "scripts" / "whatsapp-bridge"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    (persistent / "node_modules" / "old-dependency").write_text(
        "stale\n", encoding="utf-8"
    )
    (persistent / "session" / "nested").mkdir(parents=True)
    (persistent / "session" / "nested" / "creds.json").write_text(
        "complete-user-state\n", encoding="utf-8"
    )

    context = multiprocessing.get_context("spawn")
    install_release = context.Event()
    npm_entries = context.Queue()
    results = context.Queue()
    first = context.Process(
        target=_multiprocess_resolver_worker,
        args=(
            str(bundled),
            str(hermes_home),
            install_release,
            npm_entries,
            results,
        ),
    )
    second = None
    try:
        first.start()
        first_npm_pid = npm_entries.get(timeout=15)
        assert first_npm_pid == first.pid

        # The first process now holds the transaction lock inside simulated
        # npm. Release the second process from a barrier while that lock is held.
        second_start = context.Barrier(2)
        second = context.Process(
            target=_multiprocess_resolver_worker,
            args=(
                str(bundled),
                str(hermes_home),
                install_release,
                npm_entries,
                results,
                second_start,
            ),
        )
        second.start()
        second_start.wait(timeout=15)

        # Without the sibling OS lock, process two reaches npm against a second
        # stale snapshot while process one is paused. With the lock it cannot
        # enter, finish, or promote until process one commits and releases.
        with pytest.raises(queue.Empty):
            npm_entries.get(timeout=1.0)
        assert second.is_alive()

        install_release.set()
        observed_results = [results.get(timeout=15), results.get(timeout=15)]
        first.join(timeout=15)
        second.join(timeout=15)

        assert first.exitcode == 0
        assert second.exitcode == 0
        assert sorted(observed_results) == [
            ("ok", str(persistent)),
            ("ok", str(persistent)),
        ]
        with pytest.raises(queue.Empty):
            npm_entries.get_nowait()
    finally:
        install_release.set()
        for process in (first, second):
            if process is None:
                continue
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        npm_entries.close()
        results.close()

    for name in _RUNTIME_FILES:
        assert (persistent / name).read_bytes() == (bundled / name).read_bytes()
    assert (persistent / "node_modules" / "installed-version").read_text(
        encoding="utf-8"
    ) == "complete\n"
    assert not (persistent / "node_modules" / "old-dependency").exists()
    assert (persistent / "node_modules" / ".hermes-pkg-hash").read_text(
        encoding="utf-8"
    ) == whatsapp_common.whatsapp_bridge_dependency_fingerprint(persistent)
    assert (persistent / "session" / "nested" / "creds.json").read_text(
        encoding="utf-8"
    ) == "complete-user-state\n"
    assert _staging_leftovers(persistent) == []


def test_default_resolver_finds_wheel_data_runtime_under_sys_prefix(
    tmp_path, monkeypatch
):
    fake_site_packages = tmp_path / "venv" / "lib" / "python" / "site-packages"
    fake_common = (
        fake_site_packages / "gateway" / "platforms" / "whatsapp_common.py"
    )
    fake_common.parent.mkdir(parents=True)
    wheel_runtime = tmp_path / "venv" / "scripts" / "whatsapp-bridge"
    _seed_runtime(wheel_runtime, "1.0.0")

    monkeypatch.setattr(whatsapp_common, "__file__", str(fake_common))
    monkeypatch.setattr(
        sysconfig,
        "get_path",
        lambda name: str(tmp_path / "venv") if name == "data" else None,
    )

    assert whatsapp_common.resolve_whatsapp_bridge_dir(install_writable=True) == wheel_runtime


def test_failed_npm_ci_keeps_old_functional_runtime(
    tmp_path, monkeypatch, caplog
):
    bundled = tmp_path / "install"
    persistent = tmp_path / "persistent"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    (persistent / "node_modules" / "working-old-runtime").write_text(
        "keep\n", encoding="utf-8"
    )
    (persistent / "auth" / "creds").mkdir(parents=True)
    (persistent / "auth" / "creds" / "me.json").write_text(
        "auth-state\n", encoding="utf-8"
    )

    monkeypatch.setattr(whatsapp_common, "find_node_executable", lambda name: "npm")
    monkeypatch.setattr(
        whatsapp_common.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args[0], 1, stdout="", stderr="registry unavailable"
        ),
    )
    caplog.set_level(logging.WARNING, logger=whatsapp_common.logger.name)

    resolved = whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=bundled,
        persistent_bridge=persistent,
        install_writable=False,
    )

    assert resolved == persistent
    assert (persistent / "bridge.js").read_text(encoding="utf-8") == "// bridge v1.0.0\n"
    assert (persistent / "node_modules" / "working-old-runtime").exists()
    # A failed update must not bless an unknown legacy tree with a new stamp.
    assert not (persistent / "node_modules" / ".hermes-pkg-hash").exists()
    assert (persistent / "auth" / "creds" / "me.json").read_text(
        encoding="utf-8"
    ) == "auth-state\n"
    assert "keeping the existing functional persistent runtime" in caplog.text.lower()
    assert "registry unavailable" in caplog.text
    assert _staging_leftovers(persistent) == []


def test_swap_failure_rolls_back_old_runtime(tmp_path, monkeypatch, caplog):
    bundled = tmp_path / "install"
    persistent = tmp_path / "persistent"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    (persistent / "node_modules" / "working-old-runtime").write_text(
        "keep\n", encoding="utf-8"
    )
    (persistent / "session").mkdir()
    (persistent / "session" / "creds.json").write_text("auth\n", encoding="utf-8")

    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(whatsapp_common, "find_node_executable", lambda name: "npm")
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )
    real_replace = os.replace

    def fail_staging_promotion(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if ".staging-" in source_path.name and destination_path == persistent:
            raise OSError("simulated promotion failure")
        return real_replace(source, destination)

    monkeypatch.setattr(whatsapp_common.os, "replace", fail_staging_promotion)
    caplog.set_level(logging.WARNING, logger=whatsapp_common.logger.name)

    assert whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=bundled,
        persistent_bridge=persistent,
        install_writable=False,
    ) == persistent

    assert (persistent / "bridge.js").read_text(encoding="utf-8") == "// bridge v1.0.0\n"
    assert (persistent / "node_modules" / "working-old-runtime").exists()
    assert (persistent / "session" / "creds.json").read_text(
        encoding="utf-8"
    ) == "auth\n"
    assert "rolled back" in caplog.text.lower()
    assert "simulated promotion failure" in caplog.text
    assert _staging_leftovers(persistent) == []


def test_staging_tamper_is_not_promoted(tmp_path, monkeypatch, caplog):
    bundled = tmp_path / "install"
    persistent = tmp_path / "persistent"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    (persistent / "node_modules" / "working-old-runtime").write_text(
        "keep\n", encoding="utf-8"
    )

    calls: list[tuple[list[str], Path]] = []
    successful_ci = _successful_npm_ci(calls)

    def tampering_npm_ci(*args, **kwargs):
        result = successful_ci(*args, **kwargs)
        (Path(kwargs["cwd"]).parent / "bridge.js").write_text(
            "// unexpectedly modified during install\n", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(whatsapp_common, "find_node_executable", lambda name: "npm")
    monkeypatch.setattr(whatsapp_common.subprocess, "run", tampering_npm_ci)
    caplog.set_level(logging.WARNING, logger=whatsapp_common.logger.name)

    assert whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=bundled,
        persistent_bridge=persistent,
        install_writable=False,
    ) == persistent

    assert (persistent / "bridge.js").read_text(encoding="utf-8") == "// bridge v1.0.0\n"
    assert (persistent / "node_modules" / "working-old-runtime").exists()
    assert "changed while npm ci ran" in caplog.text
    assert _staging_leftovers(persistent) == []


def test_staging_creation_failure_keeps_old_runtime(tmp_path, monkeypatch, caplog):
    bundled = tmp_path / "install"
    persistent = tmp_path / "persistent"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    (persistent / "node_modules" / "working-old-runtime").write_text(
        "keep\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        tempfile,
        "mkdtemp",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    caplog.set_level(logging.WARNING, logger=whatsapp_common.logger.name)

    assert whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=bundled,
        persistent_bridge=persistent,
        install_writable=False,
    ) == persistent

    assert (persistent / "bridge.js").read_text(encoding="utf-8") == "// bridge v1.0.0\n"
    assert (persistent / "node_modules" / "working-old-runtime").exists()
    assert "disk full" in caplog.text
    assert "keeping the existing functional persistent runtime" in caplog.text.lower()


def test_post_promotion_runtime_tamper_rolls_back_before_commit(
    tmp_path, monkeypatch, caplog
):
    bundled = tmp_path / "install"
    persistent = tmp_path / "persistent"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    (persistent / "node_modules" / "working-old-runtime").write_text(
        "keep\n", encoding="utf-8"
    )
    calls = []
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )
    real_replace = os.replace

    def tamper_promoted_runtime(source, destination):
        result = real_replace(source, destination)
        if ".persistent.staging-" in Path(source).name and Path(destination) == persistent:
            (persistent / "package-lock.json").write_text(
                '{"tampered": true}\n', encoding="utf-8"
            )
        return result

    monkeypatch.setattr(whatsapp_common.os, "replace", tamper_promoted_runtime)
    caplog.set_level(logging.WARNING, logger=whatsapp_common.logger.name)

    assert whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=bundled,
        persistent_bridge=persistent,
        install_writable=False,
    ) == persistent
    assert (persistent / "bridge.js").read_text(encoding="utf-8") == "// bridge v1.0.0\n"
    assert (persistent / "node_modules" / "working-old-runtime").exists()
    assert "fingerprint changed before commit" in caplog.text
    assert "rolled back to the prior runtime" in caplog.text


def test_runtime_rollback_failure_is_typed_and_preserves_both_diagnostics(
    tmp_path, monkeypatch, caplog
):
    bundled = tmp_path / "install"
    persistent = tmp_path / "persistent"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    calls = []
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )
    real_replace = os.replace

    def tamper_then_block_rollback(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path == persistent and ".persistent.staging-" in destination_path.name:
            raise OSError("simulated rollback quarantine failure")
        result = real_replace(source, destination)
        if ".persistent.staging-" in source_path.name and destination_path == persistent:
            (persistent / "package-lock.json").write_text(
                '{"tampered": true}\n', encoding="utf-8"
            )
        return result

    monkeypatch.setattr(whatsapp_common.os, "replace", tamper_then_block_rollback)
    caplog.set_level(logging.ERROR, logger=whatsapp_common.logger.name)

    with pytest.raises(
        whatsapp_common.WhatsAppBridgeStateError,
        match="rollback also failed",
    ) as error:
        whatsapp_common.resolve_whatsapp_bridge_dir(
            bundled_bridge=bundled,
            persistent_bridge=persistent,
            install_writable=False,
        )

    detail = str(error.value)
    assert "fingerprint changed before commit" in detail
    assert "simulated rollback quarantine failure" in detail
    assert "Recovery data was preserved" in detail
    assert persistent.exists()
    assert list(tmp_path.glob(".persistent.backup-*"))
    assert "fingerprint changed before commit" in caplog.text
    assert "simulated rollback quarantine failure" in caplog.text


def test_runtime_backup_cleanup_failure_warns_and_preserves_recovery(
    tmp_path, monkeypatch, caplog
):
    bundled = tmp_path / "install"
    persistent = tmp_path / "persistent"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    (persistent / "state.json").write_text("state\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        whatsapp_common.subprocess, "run", _successful_npm_ci(calls)
    )
    real_remove = whatsapp_common._remove_path_without_following

    def fail_runtime_backup_cleanup(path):
        if Path(path).name.startswith(".persistent.backup-"):
            raise OSError("cleanup https://user:secret@invalid/backup failed")
        return real_remove(path)

    monkeypatch.setattr(
        whatsapp_common, "_remove_path_without_following", fail_runtime_backup_cleanup
    )
    caplog.set_level(logging.WARNING, logger=whatsapp_common.logger.name)

    assert whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=bundled,
        persistent_bridge=persistent,
        install_writable=False,
    ) == persistent
    backups = list(tmp_path.glob(".persistent.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "state.json").read_text(encoding="utf-8") == "state\n"
    assert "state-bearing backup remains" in caplog.text
    assert "secret" not in caplog.text
    assert "<redacted-url>" in caplog.text


def test_runtime_staging_cleanup_failure_warns_and_preserves_recovery(
    tmp_path, monkeypatch, caplog
):
    bundled = tmp_path / "install"
    persistent = tmp_path / "persistent"
    _seed_runtime(bundled, "2.0.0")
    _seed_runtime(persistent, "1.0.0")
    (persistent / "node_modules").mkdir()
    monkeypatch.setattr(
        whatsapp_common.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(
            command, 1, stdout="", stderr="primary npm failure"
        ),
    )
    real_remove = whatsapp_common._remove_path_without_following

    def fail_runtime_staging_cleanup(path):
        if Path(path).name.startswith(".persistent.staging-"):
            raise OSError("staging cleanup failed")
        return real_remove(path)

    monkeypatch.setattr(
        whatsapp_common, "_remove_path_without_following", fail_runtime_staging_cleanup
    )
    caplog.set_level(logging.WARNING, logger=whatsapp_common.logger.name)

    assert whatsapp_common.resolve_whatsapp_bridge_dir(
        bundled_bridge=bundled,
        persistent_bridge=persistent,
        install_writable=False,
    ) == persistent
    assert list(tmp_path.glob(".persistent.staging-*"))
    assert "state-bearing bridge staging" in caplog.text
    assert "remains available for recovery" in caplog.text
    assert "primary npm failure" in caplog.text


def test_adapter_constructor_and_connect_catch_typed_runtime_busy(
    monkeypatch,
):
    from gateway.config import PlatformConfig

    busy = whatsapp_common.WhatsAppBridgeBusyError("runtime transaction is busy")
    monkeypatch.setattr(whatsapp_adapter.WhatsAppAdapter, "_DEFAULT_BRIDGE_DIR", None)
    monkeypatch.setattr(
        whatsapp_adapter,
        "resolve_whatsapp_bridge_dir",
        lambda: (_ for _ in ()).throw(busy),
    )

    adapter = whatsapp_adapter.WhatsAppAdapter(PlatformConfig(enabled=True))
    assert adapter._bridge_script is None
    assert adapter._bridge_resolution_error is busy


@pytest.mark.asyncio
async def test_adapter_connect_returns_clean_retryable_state_after_busy_constructor(
    monkeypatch,
):
    from gateway.config import PlatformConfig

    busy = whatsapp_common.WhatsAppBridgeBusyError("runtime transaction is busy")
    monkeypatch.setattr(whatsapp_adapter.WhatsAppAdapter, "_DEFAULT_BRIDGE_DIR", None)
    monkeypatch.setattr(
        whatsapp_adapter,
        "resolve_whatsapp_bridge_dir",
        lambda: (_ for _ in ()).throw(busy),
    )
    adapter = whatsapp_adapter.WhatsAppAdapter(PlatformConfig(enabled=True))

    assert await adapter.connect() is False
    assert adapter.has_fatal_error


def test_plugin_adapter_uses_only_the_shared_public_resolver():
    """The plugin must not retain a second transactional resolver implementation."""
    assert (
        whatsapp_adapter.resolve_whatsapp_bridge_dir
        is whatsapp_common.resolve_whatsapp_bridge_dir
    )
    assert (
        whatsapp_adapter.ensure_whatsapp_bridge_dependencies
        is whatsapp_common.ensure_whatsapp_bridge_dependencies
    )
    duplicated_names = {
        "_resolve_bridge",
        "_bridge_package_version",
        "_bridge_runtime_signature",
        "_bridge_dir_is_writable",
        "_copy_persistent_bridge_state",
        "_copy_bundled_bridge_sources",
        "_stamp_existing_bridge_dependencies",
        "_BRIDGE_RUNTIME_SOURCE_FILES",
        "_BRIDGE_PROJECT_TEST_FILES",
    }
    assert duplicated_names.isdisjoint(vars(whatsapp_adapter))
