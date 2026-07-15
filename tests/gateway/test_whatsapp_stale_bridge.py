"""Tests for the WhatsApp stale-bridge staleness handshake.

Regression tests for the stale-bridge trap: ``connect()`` reused any
already-running bridge with ``status: connected`` unconditionally, and
``disconnect()`` only kills bridges the adapter spawned itself.  A
long-lived bridge process therefore survived gateway restarts AND
``hermes update``, serving pre-update bridge.js behavior forever (e.g.
no inbound media download → images/voice notes arrive as placeholders).

The fix: bridge.js reports a stable aggregate source hash in ``/health``
(``scriptHash``) across the manifest-declared managed runtime inventory.
Custom bridges without ``hermesRuntimeFiles`` retain the original bridge.js
plus optional helper handshake. Bridges that predate the handshake report no
hash and are treated as stale by definition.

Also covers the dependency-refresh stamp: deps are reinstalled when either
package.json or package-lock.json changes.
"""

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _make_adapter(bridge_script: str = "/tmp/test-bridge.js",
                  session_path: Path = Path("/tmp/test-wa-session")):
    """Create a WhatsAppAdapter with test attributes (bypass __init__)."""
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = MagicMock()
    adapter._bridge_port = 19876
    adapter._bridge_script = bridge_script
    adapter._session_path = session_path
    adapter._bridge_log_fh = None
    adapter._bridge_log = None
    adapter._bridge_process = None
    adapter._reply_prefix = None
    adapter._send_read_receipts = False
    adapter._running = False
    adapter._message_handler = None
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._fatal_error_handler = None
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._background_tasks = set()
    adapter._auto_tts_disabled_chats = set()
    adapter._message_queue = asyncio.Queue()
    adapter._http_session = None
    return adapter


def _mock_health(json_data):
    """Mock aiohttp.ClientSession whose GET returns 200 + *json_data*."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_AsyncCM(mock_resp))
    mock_session.close = AsyncMock()
    return MagicMock(return_value=_AsyncCM(mock_session))


def _setup_bridge_dir(tmp_path: Path) -> Path:
    """Create a complete managed bridge runtime plus paired credentials."""
    bridge_dir = tmp_path / "whatsapp-bridge"
    bridge_dir.mkdir()
    from gateway.platforms.whatsapp_common import WHATSAPP_BRIDGE_RUNTIME_FILES

    for name in WHATSAPP_BRIDGE_RUNTIME_FILES:
        if name == "package.json":
            content = json.dumps(
                {
                    "name": "bridge",
                    "version": "1.0.0",
                    "hermesRuntimeFiles": list(WHATSAPP_BRIDGE_RUNTIME_FILES),
                }
            ) + "\n"
        elif name == "package-lock.json":
            content = json.dumps(
                {
                    "name": "bridge",
                    "version": "1.0.0",
                    "lockfileVersion": 3,
                    "packages": {"": {"version": "1.0.0"}},
                }
            ) + "\n"
        else:
            content = f"// current {name}\n"
        (bridge_dir / name).write_text(content, encoding="utf-8")
    session_path = tmp_path / "session"
    session_path.mkdir()
    (session_path / "creds.json").write_text("{}")
    return bridge_dir


def _fresh_node_modules(bridge_dir: Path) -> None:
    """Create node_modules with a stamp matching both dependency manifests."""
    from gateway.platforms.whatsapp_common import (
        whatsapp_bridge_dependency_fingerprint,
    )

    nm = bridge_dir / "node_modules"
    nm.mkdir()
    (nm / ".hermes-pkg-hash").write_text(
        whatsapp_bridge_dependency_fingerprint(bridge_dir)
    )


class TestFileContentHash:
    def test_hashes_file(self, tmp_path):
        from plugins.platforms.whatsapp.adapter import _file_content_hash

        f = tmp_path / "x.js"
        f.write_text("abc")
        h = _file_content_hash(f)
        assert len(h) == 16
        assert h == _file_content_hash(f)  # deterministic


class TestBridgeSourceHash:
    def test_matches_node_complete_runtime_hash_algorithm(self, tmp_path):
        """Hash every managed runtime file in the shared inventory order."""
        from gateway.platforms.whatsapp_common import WHATSAPP_BRIDGE_RUNTIME_FILES
        from plugins.platforms.whatsapp.adapter import _bridge_source_hash

        runtime_bytes = []
        for name in WHATSAPP_BRIDGE_RUNTIME_FILES:
            if name == "package.json":
                content = (
                    json.dumps(
                        {
                            "name": "bridge",
                            "version": "1.0.0",
                            "hermesRuntimeFiles": list(
                                WHATSAPP_BRIDGE_RUNTIME_FILES
                            ),
                        }
                    )
                    + "\n"
                ).encode()
            else:
                content = f"{name}:v1\n".encode()
            (tmp_path / name).write_bytes(content)
            runtime_bytes.append(content)

        expected_digest = hashlib.sha256(b"hermes-whatsapp-framed-files-v1\0")
        for name, content in zip(
            WHATSAPP_BRIDGE_RUNTIME_FILES, runtime_bytes, strict=True
        ):
            name_bytes = name.encode("utf-8")
            expected_digest.update(len(name_bytes).to_bytes(4, "big"))
            expected_digest.update(name_bytes)
            expected_digest.update(len(content).to_bytes(8, "big"))
            expected_digest.update(content)
        expected = expected_digest.hexdigest()[:16]
        assert _bridge_source_hash(tmp_path / "bridge.js") == expected

    @pytest.mark.parametrize(
        "changed_file",
        [
            "bridge.js",
            "bridge_helpers.js",
            "allowlist.js",
            "outbound_ids.js",
            "owner_message_gate.js",
            "package.json",
            "package-lock.json",
        ],
    )
    def test_changes_when_any_managed_runtime_file_changes(self, tmp_path, changed_file):
        from plugins.platforms.whatsapp.adapter import _bridge_source_hash

        bridge_dir = _setup_bridge_dir(tmp_path)
        bridge = bridge_dir / "bridge.js"
        before = _bridge_source_hash(bridge)
        (bridge_dir / changed_file).write_text("changed\n", encoding="utf-8")
        assert _bridge_source_hash(bridge) != before

    @pytest.mark.parametrize("missing_file", ["allowlist.js", "package.json"])
    def test_missing_managed_runtime_file_is_invalid(
        self, tmp_path, missing_file
    ):
        from plugins.platforms.whatsapp.adapter import _bridge_source_hash

        bridge_dir = _setup_bridge_dir(tmp_path)
        (bridge_dir / missing_file).unlink()
        assert _bridge_source_hash(bridge_dir / "bridge.js") == ""

    def test_malformed_present_package_is_invalid(self, tmp_path):
        from plugins.platforms.whatsapp.adapter import _bridge_source_hash

        bridge_dir = _setup_bridge_dir(tmp_path)
        (bridge_dir / "package.json").write_text("{not valid json\n", encoding="utf-8")
        assert _bridge_source_hash(bridge_dir / "bridge.js") == ""

    def test_custom_bridge_without_helper_is_single_file_compatible(self, tmp_path):
        import hashlib

        from plugins.platforms.whatsapp.adapter import _bridge_source_hash

        custom = tmp_path / "custom-bridge.js"
        custom_bytes = b"const custom = true;\n"
        custom.write_bytes(custom_bytes)
        assert _bridge_source_hash(custom) == hashlib.sha256(custom_bytes).hexdigest()[:16]


def test_python_and_node_manifest_validators_have_exact_rejection_parity():
    from gateway.platforms.whatsapp_common import _validated_runtime_files
    from hermes_constants import find_node_executable

    required = [
        "bridge.js",
        "bridge_helpers.js",
        "package.json",
        "package-lock.json",
    ]
    cases = [
        {"hermesRuntimeFiles": required},
        {"hermesRuntimeFiles": [*required, " extra.js"]},
        {"hermesRuntimeFiles": [*required, "extra.js\t"]},
        {"hermesRuntimeFiles": [*required, "\u00a0extra.js"]},
        {"hermesRuntimeFiles": [*required, "\u001cextra.js"]},
        {"hermesRuntimeFiles": [*required, "nested/extra.js"]},
        {"hermesRuntimeFiles": [*required, "nested\\extra.js"]},
        {"hermesRuntimeFiles": [*required, "extra\x00.js"]},
        {"hermesRuntimeFiles": [*required, "/absolute.js"]},
        {"hermesRuntimeFiles": [*required, "."]},
        {"hermesRuntimeFiles": [*required, ".."]},
        {"hermesRuntimeFiles": [*required, "bridge.js"]},
        {"hermesRuntimeFiles": [*required, 7]},
        {"hermesRuntimeFiles": "bridge.js"},
        {"hermesRuntimeFiles": []},
        {},
        None,
        7,
        [],
    ]
    python_results = []
    for case in cases:
        validated = _validated_runtime_files(case)
        python_results.append(list(validated) if validated else None)

    node = find_node_executable("node")
    assert node, "Node is required for the Python/Node bridge manifest parity test"
    helper_uri = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "whatsapp-bridge"
        / "bridge_helpers.js"
    ).as_uri()
    script = """
const { validatedRuntimeFiles } = await import(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(cases.map(item => validatedRuntimeFiles(item))));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, helper_uri, json.dumps(cases)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert json.loads(result.stdout) == python_results
    assert python_results[0] == required
    assert all(value is None for value in python_results[1:])


def test_python_and_node_runtime_and_dependency_hashes_have_exact_parity(tmp_path):
    from gateway.platforms.whatsapp_common import (
        whatsapp_bridge_dependency_fingerprint,
        whatsapp_bridge_source_hash,
    )
    from hermes_constants import find_node_executable

    bridge_dir = _setup_bridge_dir(tmp_path)
    python_values = {
        "runtime": whatsapp_bridge_source_hash(bridge_dir / "bridge.js"),
        "dependencies": whatsapp_bridge_dependency_fingerprint(bridge_dir),
    }
    node = find_node_executable("node")
    assert node, "Node is required for the Python/Node bridge hash parity test"
    helper_uri = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "whatsapp-bridge"
        / "bridge_helpers.js"
    ).as_uri()
    script = """
const { bridgeSourceHash, bridgeDependencyFingerprint } = await import(process.argv[1]);
const directory = process.argv[2];
process.stdout.write(JSON.stringify({
  runtime: bridgeSourceHash(`${directory}/bridge.js`),
  dependencies: bridgeDependencyFingerprint(directory),
}));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, helper_uri, str(bridge_dir)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert json.loads(result.stdout) == python_values


class TestStaleBridgeHandshake:


    @pytest.mark.asyncio
    async def test_reuses_bridge_when_hash_matches(self, tmp_path):
        from plugins.platforms.whatsapp.adapter import _bridge_source_hash


        bridge_dir = _setup_bridge_dir(tmp_path)
        _fresh_node_modules(bridge_dir)
        adapter = _make_adapter(
            bridge_script=str(bridge_dir / "bridge.js"),
            session_path=tmp_path / "session",
        )
        disk_hash = _bridge_source_hash(bridge_dir / "bridge.js")
        mock_client = _mock_health({"status": "connected", "scriptHash": disk_hash})

        with patch("plugins.platforms.whatsapp.adapter.check_whatsapp_requirements", return_value=True), \
             patch("aiohttp.ClientSession", mock_client), \
             patch("plugins.platforms.whatsapp.adapter.asyncio.create_task") as mock_task, \
             patch("subprocess.Popen") as mock_popen, \
             patch.object(adapter, "_acquire_platform_lock", return_value=True, create=True), \
             patch.object(adapter, "_mark_connected", create=True):
            mock_task.side_effect = lambda coro: (coro.close() or MagicMock())
            result = await adapter.connect()

        assert result is True
        mock_popen.assert_not_called()  # reused, never spawned
        mock_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_restarts_bridge_when_read_receipt_config_changed(self, tmp_path):
        from plugins.platforms.whatsapp.adapter import _bridge_source_hash

        bridge_dir = _setup_bridge_dir(tmp_path)
        _fresh_node_modules(bridge_dir)
        adapter = _make_adapter(
            bridge_script=str(bridge_dir / "bridge.js"),
            session_path=tmp_path / "session",
        )
        adapter._send_read_receipts = True
        disk_hash = _bridge_source_hash(bridge_dir / "bridge.js")
        mock_client = _mock_health(
            {
                "status": "connected",
                "scriptHash": disk_hash,
                "sendReadReceipts": False,
            }
        )
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1

        with patch("plugins.platforms.whatsapp.adapter.check_whatsapp_requirements", return_value=True), \
             patch("aiohttp.ClientSession", mock_client), \
             patch("plugins.platforms.whatsapp.adapter.asyncio.sleep", new_callable=AsyncMock), \
             patch("plugins.platforms.whatsapp.adapter._kill_stale_bridge_by_pidfile"), \
             patch("plugins.platforms.whatsapp.adapter._kill_port_process"), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch.object(adapter, "_acquire_platform_lock", return_value=True, create=True):
            await adapter.connect()

        mock_popen.assert_called_once()

    @pytest.mark.asyncio
    async def test_restarts_bridge_on_hash_mismatch(self, tmp_path):
        bridge_dir = _setup_bridge_dir(tmp_path)
        _fresh_node_modules(bridge_dir)
        adapter = _make_adapter(
            bridge_script=str(bridge_dir / "bridge.js"),
            session_path=tmp_path / "session",
        )
        mock_client = _mock_health(
            {"status": "connected", "scriptHash": "deadbeefdeadbeef"}
        )
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1

        with patch("plugins.platforms.whatsapp.adapter.check_whatsapp_requirements", return_value=True), \
             patch("aiohttp.ClientSession", mock_client), \
             patch("plugins.platforms.whatsapp.adapter.asyncio.sleep", new_callable=AsyncMock), \
             patch("plugins.platforms.whatsapp.adapter._kill_stale_bridge_by_pidfile"), \
             patch("plugins.platforms.whatsapp.adapter._kill_port_process") as mock_kill_port, \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch.object(adapter, "_acquire_platform_lock", return_value=True, create=True):
            result = await adapter.connect()

        assert result is False
        mock_popen.assert_called_once()
        mock_kill_port.assert_called_once_with(adapter._bridge_port)


class TestDepRefreshStamp:
    @pytest.mark.asyncio
    async def test_skips_install_when_stamp_fresh(self, tmp_path):
        bridge_dir = _setup_bridge_dir(tmp_path)
        _fresh_node_modules(bridge_dir)
        adapter = _make_adapter(
            bridge_script=str(bridge_dir / "bridge.js"),
            session_path=tmp_path / "session",
        )
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1

        with patch("plugins.platforms.whatsapp.adapter.check_whatsapp_requirements", return_value=True), \
             patch("aiohttp.ClientSession", _mock_health({"status": "disconnected"})), \
             patch("plugins.platforms.whatsapp.adapter.asyncio.sleep", new_callable=AsyncMock), \
             patch("plugins.platforms.whatsapp.adapter._kill_stale_bridge_by_pidfile"), \
             patch("plugins.platforms.whatsapp.adapter._kill_port_process"), \
             patch("subprocess.run") as mock_run, \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(adapter, "_acquire_platform_lock", return_value=True, create=True):
            await adapter.connect()

        mock_run.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("changed_manifest", ["package.json", "package-lock.json"])
    async def test_reinstalls_when_dependency_manifest_changed(
        self, tmp_path, changed_manifest
    ):
        bridge_dir = _setup_bridge_dir(tmp_path)
        _fresh_node_modules(bridge_dir)
        # Simulate `hermes update` changing either the declaration or exact lock.
        (bridge_dir / changed_manifest).write_text(
            '{"name": "bridge", "version": "2.0.0"}\n', encoding="utf-8"
        )
        adapter = _make_adapter(
            bridge_script=str(bridge_dir / "bridge.js"),
            session_path=tmp_path / "session",
        )
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1

        with patch("plugins.platforms.whatsapp.adapter.check_whatsapp_requirements", return_value=True), \
             patch("aiohttp.ClientSession", _mock_health({"status": "disconnected"})), \
             patch("plugins.platforms.whatsapp.adapter.asyncio.sleep", new_callable=AsyncMock), \
             patch("plugins.platforms.whatsapp.adapter._kill_stale_bridge_by_pidfile"), \
             patch("plugins.platforms.whatsapp.adapter._kill_port_process"), \
             patch("subprocess.run") as mock_run, \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(adapter, "_acquire_platform_lock", return_value=True, create=True):
            def install_in_staging(*args, **kwargs):
                (Path(kwargs["cwd"]) / "node_modules").mkdir()
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = install_in_staging
            await adapter.connect()

        mock_run.assert_called_once()
        assert mock_run.call_args[0][0][-2:] == ["ci", "--silent"]
        # Stamp covers package.json and package-lock.json together.
        from gateway.platforms.whatsapp_common import (
            whatsapp_bridge_dependency_fingerprint,
        )
        stamp = (bridge_dir / "node_modules" / ".hermes-pkg-hash").read_text().strip()
        assert stamp == whatsapp_bridge_dependency_fingerprint(bridge_dir)

    @pytest.mark.asyncio
    async def test_installs_when_node_modules_missing(self, tmp_path):
        bridge_dir = _setup_bridge_dir(tmp_path)  # no node_modules
        adapter = _make_adapter(
            bridge_script=str(bridge_dir / "bridge.js"),
            session_path=tmp_path / "session",
        )
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1

        def _npm_install(*args, **kwargs):
            # npm creates node_modules as a side effect
            (Path(kwargs["cwd"]) / "node_modules").mkdir(exist_ok=True)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("plugins.platforms.whatsapp.adapter.check_whatsapp_requirements", return_value=True), \
             patch("aiohttp.ClientSession", _mock_health({"status": "disconnected"})), \
             patch("plugins.platforms.whatsapp.adapter.asyncio.sleep", new_callable=AsyncMock), \
             patch("plugins.platforms.whatsapp.adapter._kill_stale_bridge_by_pidfile"), \
             patch("plugins.platforms.whatsapp.adapter._kill_port_process"), \
             patch("subprocess.run", side_effect=_npm_install) as mock_run, \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(adapter, "_acquire_platform_lock", return_value=True, create=True):
            await adapter.connect()

        mock_run.assert_called_once()



class TestCacheDirEnvPassthrough:
    @pytest.mark.asyncio
    async def test_bridge_spawn_env_has_cache_dirs(self, tmp_path):
        bridge_dir = _setup_bridge_dir(tmp_path)
        _fresh_node_modules(bridge_dir)
        adapter = _make_adapter(
            bridge_script=str(bridge_dir / "bridge.js"),
            session_path=tmp_path / "session",
        )
        adapter._send_read_receipts = True
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1

        with patch("plugins.platforms.whatsapp.adapter.check_whatsapp_requirements", return_value=True), \
             patch("aiohttp.ClientSession", _mock_health({"status": "disconnected"})), \
             patch("plugins.platforms.whatsapp.adapter.asyncio.sleep", new_callable=AsyncMock), \
             patch("plugins.platforms.whatsapp.adapter._kill_stale_bridge_by_pidfile"), \
             patch("plugins.platforms.whatsapp.adapter._kill_port_process"), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch.object(adapter, "_acquire_platform_lock", return_value=True, create=True):
            await adapter.connect()

        env = mock_popen.call_args.kwargs["env"]
        from gateway.platforms.base import (
            get_audio_cache_dir,
            get_document_cache_dir,
            get_image_cache_dir,
        )
        assert env["HERMES_IMAGE_CACHE_DIR"] == str(get_image_cache_dir())
        assert env["HERMES_AUDIO_CACHE_DIR"] == str(get_audio_cache_dir())
        assert env["HERMES_DOCUMENT_CACHE_DIR"] == str(get_document_cache_dir())
        assert env["WHATSAPP_SEND_READ_RECEIPTS"] == "true"
