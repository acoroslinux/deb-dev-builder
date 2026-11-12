import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType

import pytest
import yaml

from deb_dev_builder.core.chroot_manager import ChrootManager
from deb_dev_builder.core.customizer import SystemCustomizer
from deb_dev_builder.core.installer_audit import audit_calamares, audit_installer_root, InstallerAuditError
from deb_dev_builder.core.installer_lifecycle import prepare_target_script
from deb_dev_builder.core.path_utils import resolve_from_project
from deb_dev_builder.core.runtime_config import load_runtime


@pytest.fixture(scope="module", params=["bookworm", "trixie", "sid"])
def native_modules(tmp_path_factory, request):
    packages = sorted(resolve_from_project("cache/debian").glob(request.param + "/amd64/apt/archives/calamares_*.deb"))
    if not packages or not shutil.which("dpkg-deb"):
        pytest.skip("Cached Calamares package required for native contract tests")
    root = tmp_path_factory.mktemp("native-calamares")
    subprocess.run(["dpkg-deb", "-x", str(packages[-1]), str(root)], check=True, capture_output=True)
    return root


def deploy(root, variant):
    source = resolve_from_project("configs/custom_files/calamares")
    destination = root / "etc/calamares"
    for group in ("modules", "branding"):
        shutil.copytree(source / group, destination / group, dirs_exist_ok=True)
    shutil.copytree(source / variant / "modules", destination / "modules", dirs_exist_ok=True)
    shutil.copy2(source / variant / "settings.conf", destination / "settings.conf")
    customizer = SystemCustomizer(ChrootManager(root, mode="real"), {"with_calamares": True})
    customizer.configure_calamares()
    for binary in customizer.runtime["build"]["installer_audit"]["filesystem_tools"].values():
        path = root / binary
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return customizer.runtime


@pytest.mark.parametrize("variant", ["systemd", "non-systemd"])
def test_deployed_sequences_and_configs_match_real_installer_modules(native_modules, tmp_path, variant):
    root = tmp_path / "root"
    shutil.copytree(native_modules, root)
    runtime = deploy(root, variant)
    result = audit_calamares(root, runtime["build"]["installer_audit"])
    assert "displaymanager" in result["schemas_validated"]
    assert "fstab" in result["schemas_validated"]
    if variant == "systemd":
        assert "services-systemd" in result["schemas_validated"]


@pytest.mark.parametrize("firmware", ["efi", "bios"])
def test_native_mount_limits_firmware_variables_to_uefi(native_modules, tmp_path, monkeypatch, firmware):
    from types import SimpleNamespace

    root = tmp_path / "root"
    shutil.copytree(native_modules, root)
    deploy(root, "systemd")
    config_dir = root / "etc/calamares/modules"
    before = {path.name: path.read_bytes() for path in config_dir.glob("*.conf")}
    # Reconfiguration must preserve the translated policy without duplicating it.
    SystemCustomizer(ChrootManager(root, mode="real"), {"with_calamares": True}).configure_calamares()
    assert before == {path.name: path.read_bytes() for path in config_dir.glob("*.conf")}
    configuration = yaml.safe_load((config_dir / "mount.conf").read_text())
    state = {"firmwareType": firmware, "efiSystemPartition": "/boot/efi",
             "partitions": [{"mountPoint": "/", "fs": "ext4", "device": "/dev/test-root"}]}
    utils = SimpleNamespace(gettext_path=lambda: str(tmp_path), gettext_languages=lambda: ["en"], warning=lambda *args: None)
    lib = ModuleType("libcalamares")
    lib.utils = utils
    lib.job = SimpleNamespace(configuration=configuration)
    lib.globalstorage = SimpleNamespace(value=state.get, insert=lambda key, value: state.__setitem__(key, value))
    monkeypatch.setitem(sys.modules, "libcalamares", lib)
    module = next(root.glob("usr/lib/*/calamares/modules/mount/main.py"))
    spec = importlib.util.spec_from_file_location("native_mount", module)
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    monkeypatch.setattr(native.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path / "installed"))
    mounted = []
    monkeypatch.setattr(native, "mount_partition", lambda target, partition, *args: mounted.append(partition))
    assert native.run() is None
    assert any(entry["mountPoint"] == "/sys/firmware/efi/efivars" for entry in mounted) == (firmware == "efi")
    assert any(entry["mountPoint"] == "/dev/pts" for entry in mounted)
    if "extraMountsEfi" in configuration:
        fstab = yaml.safe_load((config_dir / "fstab.conf").read_text())
        assert fstab["efiMountOptions"] == "defaults,noatime,umask=0077"
        assert fstab["mountOptions"]["btrfs"] == "defaults,noatime,compress=zstd:3,discard=async"
        assert all(isinstance(entry.get("options", ""), str) for entry in state["extraMounts"])


@pytest.mark.parametrize("problem", ["schema", "payload", "formatter", "order", "branding-image", "branding-format"])
def test_audit_rejects_ignored_options_missing_modules_and_unsafe_sequences(native_modules, tmp_path, problem):
    root = tmp_path / "root"
    shutil.copytree(native_modules, root)
    runtime = deploy(root, "systemd")
    config = root / "etc/calamares"
    if problem == "schema":
        (config / "modules/displaymanager.conf").write_text("displaymanagers: [gdm3]\n")
    elif problem == "payload":
        next(root.glob("usr/lib/*/calamares/modules/initramfs/*.so")).unlink()
    elif problem == "formatter":
        (root / runtime["build"]["installer_audit"]["filesystem_tools"]["f2fs"]).unlink()
    elif problem == "branding-image":
        (config / "branding/deb-dev-modern/slide_speed.jpg").unlink()
    elif problem == "branding-format":
        (config / "branding/deb-dev-modern/slide_speed.jpg").write_bytes(b'\x89PNG\r\n\x1a\n')
    else:
        settings = yaml.safe_load((config / "settings.conf").read_text())
        jobs = next(stage["exec"] for stage in settings["sequence"] if "exec" in stage)
        jobs.remove("shellprocess@prepare-install-target")
        jobs.insert(jobs.index("displaymanager") + 1, "shellprocess@prepare-install-target")
        (config / "settings.conf").write_text(yaml.safe_dump(settings))
    with pytest.raises(InstallerAuditError):
        audit_calamares(root, runtime["build"]["installer_audit"])


@pytest.mark.parametrize("manager,native_name", [("sddm", "sddm"), ("lightdm", "lightdm"), ("gdm3", "gdm")])
def test_installer_configures_only_the_selected_login_manager(native_modules, tmp_path, manager, native_name):
    root = tmp_path / "root"
    shutil.copytree(native_modules, root)
    runtime = deploy(root, "non-systemd")
    SystemCustomizer(ChrootManager(root, mode="real"), {
        "with_calamares": True, "display_manager": manager,
    }).configure_calamares()
    data = yaml.safe_load((root / "etc/calamares/modules/displaymanager.conf").read_text())
    assert data["displaymanagers"] == [native_name]
    assert not data["basicSetup"] and not data["sysconfigSetup"]
    audit_calamares(root, runtime["build"]["installer_audit"])


@pytest.mark.parametrize("autologin", [None, "installed-user"])
def test_native_lightdm_honors_new_autologin_choice_after_live_cleanup(native_modules, tmp_path, monkeypatch, autologin):
    utils = ModuleType("libcalamares.utils")
    utils.gettext_path = lambda: str(tmp_path)
    utils.gettext_languages = lambda: ["en"]
    lib = ModuleType("libcalamares")
    lib.utils = utils
    monkeypatch.setitem(sys.modules, "libcalamares", lib)
    monkeypatch.setitem(sys.modules, "libcalamares.utils", utils)
    module = next(native_modules.glob("usr/lib/*/calamares/modules/displaymanager/main.py"))
    spec = importlib.util.spec_from_file_location("native_displaymanager", module)
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    root = tmp_path / "installed"
    runtime = load_runtime()
    for relative, content in runtime["desktop"]["display_managers"]["lightdm"]["files"].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.format(live_user="liveuser", session="xfce"))
    script = tmp_path / "prepare"
    script.write_bytes(prepare_target_script(runtime))
    subprocess.run(["sh", str(script), str(root)], check=True, capture_output=True)
    dm = native.DMlightdm(str(root))
    assert dm.set_autologin(autologin, autologin is not None, native.DesktopEnvironment("startxfce4", "xfce")) is None
    text = (root / "etc/lightdm/lightdm.conf").read_text()
    assert "liveuser" not in text
    assert not (root / "etc/lightdm/lightdm.conf.d/50-autologin.conf").exists()
    assert ("autologin-user=installed-user" in text) == (autologin is not None)


def test_rootfs_audit_rejects_missing_machine_id_and_writes_a_report(tmp_path):
    runtime = load_runtime()
    (tmp_path / "etc").mkdir()
    with pytest.raises(InstallerAuditError, match="machine-id"):
        audit_installer_root(tmp_path, {}, runtime)
    (tmp_path / "etc/machine-id").write_text("")
    result = audit_installer_root(tmp_path, {}, runtime)
    assert result["machine_id_placeholder"] is True
    assert (tmp_path / runtime["build"]["installer_audit"]["report_path"]).is_file()
