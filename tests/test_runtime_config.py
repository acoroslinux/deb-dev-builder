"""Operational JSON changes must reach consumers without changing Python."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli
from deb_dev_builder.core.config_loader import ConfigLoader, ConfigLoaderError
from deb_dev_builder.core.customizer import SystemCustomizer
from deb_dev_builder.core.disk_engine import DiskEngine
from deb_dev_builder.core.iso_engine import ISOEngine
from deb_dev_builder.core.orchestrator import BuildOrchestrator
from deb_dev_builder.core.runtime_config import RuntimeConfigError, load_runtime, live_user_settings
from deb_dev_builder.core.toolchain_manager import ToolchainManager


class RecordingChroot:
    mode = "real"

    def __init__(self, root):
        self.target_root = root
        self.calls = []

    def run_in_chroot(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)


def global_config(tmp_path, runtime):
    path = tmp_path / "global.json"
    path.write_text(json.dumps({"runtime": runtime}))
    return str(path)


def test_runtime_lists_replace_defaults_and_do_not_mutate_catalog():
    settings = load_runtime({"runtime": {"services": {"auto_enable": []}}})
    assert settings["services"]["auto_enable"] == []
    assert "NetworkManager" in load_runtime()["services"]["auto_enable"]


@pytest.mark.parametrize("override", [[], {"services": []}, {"services": {"auto_enable": [False]}}])
def test_invalid_override_reports_configuration_path(override):
    with pytest.raises(RuntimeConfigError, match="runtime"):
        load_runtime({"runtime": override})


def test_missing_runtime_catalog_fails_explicitly(tmp_path):
    with pytest.raises(RuntimeConfigError, match="Cannot load runtime"):
        load_runtime(path=tmp_path / "missing.json")


def test_global_policy_changes_default_profiles_bootloader_and_packages(tmp_path):
    path = global_config(tmp_path, {"build": {
        "default_live_profile": "guest", "iso_bios_bootloader": "grub2-uefi",
        "live_desktop_profiles": [], "desktop_profiles": [], "desktop_update_profiles": [],
        "firmware_profiles": [], "default_audio_profile": "pulseaudio",
        "feature_packages": {"with_flathub": ["custom-flatpak-package"]},
    }})
    supplied_profiles = []
    build = BuildOrchestrator(config_path=path, distro="debian-13", desktop="xfce", variant="live",
                              package_profiles=supplied_profiles, with_flathub=True)
    assert build.package_profiles == ["pulseaudio"]
    assert supplied_profiles == []
    assert build.config["bootloader_type"] == "grub2-uefi"
    assert build.config["live_user"]["name"] == "guest"
    assert build.config["live_user"]["groups"] == ["audio", "video"]
    assert "custom-flatpak-package" in build.config["software"]


def test_kernel_package_templates_and_architecture_policy_are_overridable(tmp_path):
    path = global_config(tmp_path, {
        "architectures": {"amd64": {"kernel_suffix": "custom", "iso_packages": ["custom-efi"]}},
        "build": {"kernel_packages": {"generic": ["custom-kernel-{suffix}"]}},
    })
    build = BuildOrchestrator(config_path=path, distro="debian-13")
    assert {"custom-kernel-custom", "custom-efi"} <= set(build.config["software"])
    assert "linux-image-amd64" not in build.config["software"]


def test_service_policy_can_replace_auto_enable_and_masks(tmp_path):
    for service in ("NetworkManager", "custom-network"):
        unit = tmp_path / f"usr/lib/systemd/system/{service}.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.touch()
    chroot = RecordingChroot(tmp_path)
    SystemCustomizer(chroot, {"variant": "live", "output_format": "iso", "runtime": {"services": {
        "auto_enable": ["custom-network"], "iso_mask": {"systemd": []},
        "live_mask": {"systemd": ["custom-wait.service"]},
    }}}).setup_services()
    assert (["systemctl", "enable", "custom-network"], {"check": False}) in chroot.calls
    assert not any("NetworkManager" in command for command, _ in chroot.calls)
    assert (tmp_path / "etc/systemd/system/custom-wait.service").readlink() == Path("/dev/null")
    assert not (tmp_path / "etc/systemd/system/systemd-firstboot.service").is_symlink()


def test_display_manager_templates_and_vendor_pam_are_preserved(tmp_path):
    pam = tmp_path / "etc/pam.d/sddm-autologin"
    pam.parent.mkdir(parents=True)
    pam.write_text("vendor PAM policy\n")
    config = {"display_manager": "sddm", "session_type": "wayland", "live_user": {"name": "demo"},
              "runtime": {"desktop": {"display_managers": {"sddm": {"files": {
                  "etc/sddm.conf": "User={live_user}\nDisplayServer={display_server}\nSession={session}\n",
              }}}, "fallback_session": "custom-session"}}}
    SystemCustomizer(RecordingChroot(tmp_path), config).configure_autologin()
    assert (tmp_path / "etc/sddm.conf").read_text() == "User=demo\nDisplayServer=x11\nSession=custom-session\n"
    assert pam.read_text() == "vendor PAM policy\n"
    assert not (tmp_path / "etc/lightdm/lightdm.conf").exists()


def test_zram_contents_and_user_shell_are_configurable(tmp_path):
    chroot = RecordingChroot(tmp_path)
    customizer = SystemCustomizer(chroot, {"runtime": {
        "users": {"shell": "/bin/sh", "autologin_group": "custom-login"},
        "customization": {"zram": {"systemd": {"content": "custom zram\n"}}},
    }})
    customizer.setup_live_users()
    customizer.configure_zram()
    create = next(command for command, _ in chroot.calls if command[0] == "useradd")
    assert create[create.index("-s") + 1] == "/bin/sh"
    assert "custom-login" in create[create.index("-G") + 1].split(",")
    assert (tmp_path / "etc/systemd/zram-generator.conf").read_text() == "custom zram\n"


def test_explicit_account_never_inherits_default_password():
    assert live_user_settings({"live_user": {"name": "demo"}})["password"] is None
    assert live_user_settings({"live_user": "demo"})["password"] is None


def test_squashfs_and_disk_geometry_use_runtime_settings(tmp_path, monkeypatch):
    calls = []
    root = tmp_path / "root"
    root.mkdir()
    toolchain = SimpleNamespace(run_tool=lambda *args, **kwargs: calls.append(args))
    config = {"runtime": {"artifacts": {
        "squashfs": {"compression": "xz", "block_size": "256K", "exclude": ["custom/*"]},
        "disk": {"root_slack_mib": 123},
    }}}
    iso = ISOEngine(tmp_path, root, "test", config, "real", toolchain)
    iso._create_squashfs(root, tmp_path / "root.squashfs")
    command = calls[0][1]
    assert command[command.index("-comp") + 1] == "xz"
    assert command[command.index("-b") + 1] == "256K"
    assert command[command.index("-e") + 1:] == ["custom/*"]
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: b"100\troot\n")
    disk = DiskEngine(tmp_path, root, "test", config, "real")
    assert disk._calculate_image_size(root) == 223


def test_toolchain_defaults_come_from_json_with_explicit_overrides(tmp_path):
    runtime = load_runtime()
    manager = ToolchainManager(tmp_path / "work/amd64", toolchain_config={"packages": ["custom-tool"]})
    assert manager.toolchain_config["packages"] == ["custom-tool"]
    assert manager.toolchain_config["devuan_keyring"] == runtime["toolchain"]["devuan_keyring"]


def test_cli_loads_defaults_from_selected_global_json(tmp_path, monkeypatch):
    path = global_config(tmp_path, {"cli": {"architecture": "aarch64", "filesystem": "btrfs", "clean": False}})
    captured = {}

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def validate(self):
            return {"valid": True}

    monkeypatch.setattr(cli, "BuildOrchestrator", FakeOrchestrator)
    monkeypatch.setattr("sys.argv", ["cli.py", "-c", path, "--validate"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert captured["arch"] == "aarch64"
    assert captured["fs_type"] == "btrfs"
    assert captured["clean"] is False


def test_devuan_keyring_pin_is_read_from_toolchain_configuration(tmp_path, monkeypatch):
    import hashlib

    package = b"fixture keyring package"
    manager = ToolchainManager(tmp_path / "work/amd64", mode="real", toolchain_config={
        "devuan_keyring": {"filename": "custom-keyring.deb", "sha256": hashlib.sha256(package).hexdigest()},
    })
    manager.project_root = tmp_path
    manager.is_mounted = True
    bundled = tmp_path / "tools/keyrings/custom-keyring.deb"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(package)
    (manager.build_host_dir / "tmp").mkdir(parents=True)
    installed = manager.build_host_dir / "usr/share/keyrings/devuan-archive-keyring.pgp"
    calls = []

    def install(command):
        calls.append(command)
        installed.parent.mkdir(parents=True)
        installed.touch()

    monkeypatch.setattr(manager, "run_in_build_host", install)
    manager.install_devuan_keyring()
    assert calls == [["dpkg", "--install", "/tmp/custom-keyring.deb"]]
    assert installed.is_file()


@pytest.fixture
def module_catalog(tmp_path):
    import shutil
    from deb_dev_builder.core.path_utils import resolve_from_project

    source = resolve_from_project("configs")
    manifest = json.loads((source / "catalog.json").read_text())
    files = {"catalog.json", *[entry["source"] for entry in manifest["documents"]]}
    for category in ("architectures", "system"):
        files.update(str(path.relative_to(source)) for path in (source / category).glob("*.json"))
    for relative in files:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, destination)
    return tmp_path


def test_profile_metadata_is_the_single_source_for_architecture_kernel_and_init(module_catalog):
    def update(relative, change):
        path = module_catalog / relative
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))

    update("architectures/amd64.json", lambda data: data["image"].update(efi_target="custom-efi"))
    update("architectures/amd64.json", lambda data: data["aliases"].append("custom-amd64"))
    update("system/generic.json", lambda data: data.update(package_templates=["profile-kernel-{suffix}"]))
    update("system/systemd.json", lambda data: data.update(zram_package="profile-zram"))
    runtime = load_runtime(path=module_catalog / "catalog.json")
    assert runtime["architectures"]["amd64"]["efi_target"] == "custom-efi"
    assert runtime["architecture_aliases"]["custom-amd64"] == "amd64"
    assert "custom-amd64" in runtime["cli"]["architectures"]
    assert ConfigLoader._architecture_kernel_packages("generic", "amd64", runtime) == ["profile-kernel-amd64"]
    assert runtime["build"]["zram_packages"]["systemd"] == "profile-zram"


def test_module_policy_changes_are_loaded_without_the_monolithic_file(module_catalog):
    policy = module_catalog / "services/policy.json"
    data = json.loads(policy.read_text())
    data["auto_enable"] = []
    policy.write_text(json.dumps(data))
    assert not (module_catalog / "runtime.json").exists()
    assert load_runtime(path=module_catalog / "catalog.json")["services"]["auto_enable"] == []


def test_repository_fallback_follows_the_default_distribution_profile(module_catalog):
    path = module_catalog / "system/debian-13.json"
    data = json.loads(path.read_text())
    data.update(mirror="https://mirror.example/debian", suite="profile-suite", components=["main"])
    path.write_text(json.dumps(data))
    runtime = load_runtime(path=module_catalog / "catalog.json")
    assert runtime["apt"] == {
        "mirror": "https://mirror.example/debian", "suite": "profile-suite",
        "components": ["main"], "distro": "debian-13",
    }
    runtime = load_runtime({"runtime": {"cli": {"distro": "devuan-6"}}}, path=module_catalog / "catalog.json")
    assert runtime["apt"]["suite"] == "excalibur"
    assert runtime["apt"]["mirror"] == "http://deb.devuan.org/merged"


def test_module_documents_are_not_selectable_profiles(module_catalog):
    assert "defaults" not in cli._available_profiles(module_catalog, "system", "init_system")
    assert "policy" not in cli._available_profiles(module_catalog, "boot", "bootloader")
    with pytest.raises(ConfigLoaderError, match="not a selectable"):
        ConfigLoader(module_catalog).load_profile("system", "defaults")


@pytest.mark.parametrize("invalid", ["missing.json", "../outside.json"])
def test_invalid_catalog_references_fail_explicitly(module_catalog, invalid):
    path = module_catalog / "catalog.json"
    data = json.loads(path.read_text())
    data["documents"][0]["source"] = invalid
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeConfigError, match="Cannot load runtime|escapes configuration"):
        load_runtime(path=path)
