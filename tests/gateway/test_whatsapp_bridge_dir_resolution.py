"""Bridge discovery is read-only; explicit maintenance owns mirror creation."""
from pathlib import Path
import sysconfig
import pytest
from gateway.platforms import whatsapp_common


def _seed_install_tree(bridge):
    bridge.mkdir(parents=True)
    (bridge / "bridge.js").write_text("// bridge")
    (bridge / "package.json").write_text('{}')


@pytest.mark.parametrize("mirror_exists,writable", [(False, False), (False, True), (True, False), (True, True)])
def test_discovery_never_writes_or_installs(tmp_path, monkeypatch, mirror_exists, writable):
    bundle = tmp_path / "install" / "scripts" / "whatsapp-bridge"
    _seed_install_tree(bundle)
    home = tmp_path / "home"
    mirror = home / "scripts" / "whatsapp-bridge"
    if mirror_exists:
        _seed_install_tree(mirror)
    monkeypatch.setattr(whatsapp_common, "__file__", str(tmp_path / "install" / "gateway" / "platforms" / "whatsapp_common.py"))
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)
    monkeypatch.setattr(whatsapp_common, "_bridge_dir_is_writable", lambda _: writable, raising=False)
    def forbidden(*a, **kw):
        pytest.fail("discovery mutated the filesystem or installed")
    for name in ("touch", "mkdir", "unlink", "write_text"):
        monkeypatch.setattr(Path, name, forbidden)
    monkeypatch.setattr(whatsapp_common.shutil, "copytree", forbidden)
    monkeypatch.setattr(whatsapp_common.subprocess, "run", forbidden)
    assert whatsapp_common.resolve_whatsapp_bridge_dir() == (mirror if mirror_exists or not writable else bundle)
    assert mirror.exists() == mirror_exists


def test_default_resolver_finds_wheel_data_runtime_under_sys_prefix(tmp_path, monkeypatch):
    fake_common = tmp_path / "venv" / "lib" / "python" / "site-packages" / "gateway" / "platforms" / "whatsapp_common.py"
    wheel_runtime = tmp_path / "venv" / "scripts" / "whatsapp-bridge"
    _seed_install_tree(wheel_runtime)
    monkeypatch.setattr(whatsapp_common, "__file__", str(fake_common))
    monkeypatch.setattr(sysconfig, "get_path", lambda name: str(tmp_path / "venv") if name == "data" else None)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path / "home")
    assert whatsapp_common.resolve_whatsapp_bridge_dir() == wheel_runtime


def test_source_layout_precedes_data_layout(tmp_path, monkeypatch):
    source = tmp_path / "source" / "scripts" / "whatsapp-bridge"
    data = tmp_path / "data" / "scripts" / "whatsapp-bridge"
    _seed_install_tree(source)
    _seed_install_tree(data)
    monkeypatch.setattr(whatsapp_common, "__file__", str(tmp_path / "source" / "gateway" / "platforms" / "whatsapp_common.py"))
    monkeypatch.setattr(sysconfig, "get_path", lambda _: str(tmp_path / "data"))
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path / "home")
    assert whatsapp_common.resolve_whatsapp_bridge_dir() == source


def test_discovery_rejects_persistent_root_alias(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    _seed_install_tree(bundle)
    home = tmp_path / "home"
    mirror = home / "scripts" / "whatsapp-bridge"
    mirror.parent.mkdir(parents=True)
    mirror.symlink_to(bundle, target_is_directory=True)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)
    with pytest.raises(whatsapp_common.WhatsAppBridgeDependencyError):
        whatsapp_common.resolve_whatsapp_bridge_dir()
