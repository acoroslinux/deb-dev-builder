import pytest
from pathlib import Path
import json

from deb_dev_builder.core.config_loader import ConfigLoader, ConfigLoaderError
from deb_dev_builder.core.path_utils import resolve_from_project

@pytest.fixture
def config_root():
    return resolve_from_project("configs")

class TestConfigLoader:
    def test_load_global_config(self, config_root):
        loader = ConfigLoader(config_root)
        config = loader.assemble_build_config(
            global_config_path=config_root / "global_build.json",
            architecture="amd64",
            distro="debian-12",
        )
        assert config.get("architecture") == "amd64" or config.get("suite") == "bookworm"

    def test_package_profiles_exist(self, config_root):
        required = [
            "base", "audio", "bluetooth", "browsers", "chat", "cloud-tools",
            "desktop-apps", "dev-tools", "development", "filesystems", "gaming",
            "graphics", "ide", "multimedia", "multimedia-editing", "network-shares",
            "network-tools", "networking", "office", "printing", "productivity",
            "security", "system-utils", "virtualization", "wayland", "xorg",
            "firmware"
        ]
        for name in required:
            path = config_root / "software" / f"{name}.json"
            assert path.exists(), f"Missing package profile: {name}.json"

    def test_all_json_documents_are_objects(self, config_root):
        for path in config_root.rglob("*.json"):
            with path.open(encoding="utf-8") as stream:
                assert isinstance(json.load(stream), dict), path

    def test_profile_packages_are_merged_into_runtime_software(self, config_root):
        config = ConfigLoader(config_root).assemble_build_config(
            config_root / "global_build.json",
            architecture="amd64",
            distro="debian-12",
            init_system="systemd",
            desktop="xfce",
            kernel="generic",
            bootloader="grub2-hybrid",
        )
        assert {"xfce4", "linux-image-amd64", "grub-pc-bin", "systemd-sysv"} <= set(config["software"])
        assert config["architecture"] == "amd64"
        assert config["dpkg_arch"] == "amd64"

    def test_aarch64_uses_debian_dpkg_architecture_name(self, config_root):
        config = ConfigLoader(config_root).assemble_build_config(
            config_root / "global_build.json", architecture="aarch64", distro="debian-12"
        )
        assert config["architecture"] == "aarch64"
        assert config["dpkg_arch"] == "arm64"
        assert "linux-image-arm64" in config["software"]
        assert "linux-image-amd64" not in config["software"]

    def test_x86_architecture_aliases_are_normalized_for_api_callers(self, config_root):
        config = ConfigLoader(config_root).assemble_build_config(
            config_root / "global_build.json", architecture="x86_64", distro="debian-13"
        )
        assert config["architecture"] == "amd64"
        assert config["dpkg_arch"] == "amd64"

    def test_missing_selected_profile_is_an_error(self, config_root):
        with pytest.raises(ConfigLoaderError, match="not found"):
            ConfigLoader(config_root).assemble_build_config(
                config_root / "global_build.json", architecture="amd64", distro="does-not-exist"
            )

    def test_catalog_semantics_are_valid(self, config_root):
        assert ConfigLoader(config_root).audit_catalog() == []

    def test_base_customizations_and_service_profiles_are_loaded(self, config_root):
        config = ConfigLoader(config_root).assemble_build_config(
            config_root / "global_build.json",
            architecture="amd64",
            distro="debian-13",
            service_profiles=["base"],
            live_profile="admin",
        )
        assert config["live_user"]["name"] == "admin"
        assert "cron" in config["services"]["enable"]
        assert "cron" in config["software"]

    def test_guest_profile_does_not_inherit_sudo(self, config_root):
        config = ConfigLoader(config_root).assemble_build_config(
            config_root / "global_build.json",
            architecture="amd64",
            distro="debian-13",
            live_profile="guest",
        )
        assert config["live_user"]["name"] == "guest"
        assert config["live_user"]["groups"] == ["audio", "video"]

    def test_minimal_variant_removes_nonessential_convenience_packages(self, config_root):
        config = ConfigLoader(config_root).assemble_build_config(
            config_root / "global_build.json",
            architecture="amd64",
            distro="debian-13",
            variant="minimal",
        )
        assert not {"curl", "wget", "sudo", "user-setup", "zstd"} & set(config["software"])

    def test_hardware_profile_is_loaded_and_architecture_checked(self, config_root):
        loader = ConfigLoader(config_root)
        config = loader.assemble_build_config(
            config_root / "global_build.json",
            architecture="aarch64",
            distro="debian-13",
            hardware_profile="pinebookpro",
        )
        assert config["hardware_profile"] == "pinebookpro"
        assert config["hardware"]["partition_start_mib"] == 16
        with pytest.raises(ConfigLoaderError, match="requires architecture"):
            loader.assemble_build_config(
                config_root / "global_build.json",
                architecture="amd64",
                distro="debian-13",
                hardware_profile="pinebookpro",
            )

    def test_vm_profile_adds_guest_packages(self, config_root):
        config = ConfigLoader(config_root).assemble_build_config(
            config_root / "global_build.json",
            architecture="amd64",
            distro="debian-13",
            vm_profile="qemu",
        )
        assert config["vm"]["output_format"] == "qcow2"
        assert "qemu-guest-agent" in config["software"]

    @pytest.mark.parametrize("profile,architecture", [
        ("asahi", "aarch64"),
        ("generic-uefi", "amd64"),
        ("odroid-n2", "aarch64"),
        ("pinebookpro", "aarch64"),
        ("rockpro64", "aarch64"),
        ("rpi4", "aarch64"),
        ("visionfive2", "riscv64"),
    ])
    def test_every_hardware_profile_composes(self, config_root, profile, architecture):
        config = ConfigLoader(config_root).assemble_build_config(
            config_root / "global_build.json",
            architecture=architecture,
            distro="debian-13",
            hardware_profile=profile,
        )
        assert config["hardware_profile"] == profile

    @pytest.mark.parametrize("profile,architecture,package", [
        ("qemu", "amd64", "qemu-guest-agent"),
        ("virtualbox", "amd64", None),
        ("vmware", "aarch64", "open-vm-tools"),
        ("hyperv", "amd64", "hyperv-daemons"),
    ])
    def test_every_vm_profile_composes(self, config_root, profile, architecture, package):
        config = ConfigLoader(config_root).assemble_build_config(
            config_root / "global_build.json",
            architecture=architecture,
            distro="debian-13",
            vm_profile=profile,
        )
        assert config["vm_profile"] == profile
        if package:
            assert package in config["software"]

    def test_wayland_desktop_does_not_pull_xorg_profile(self, config_root):
        config = ConfigLoader(config_root).assemble_build_config(
            config_root / "global_build.json", architecture="amd64", distro="debian-13", desktop="sway"
        )
        assert config["session_type"] == "wayland"
        assert "waybar" in config["software"]
        assert "xorg" not in config["software"]
