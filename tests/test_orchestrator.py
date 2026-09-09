import pytest
from pathlib import Path
from deb_dev_builder.core.orchestrator import BuildOrchestrator

def make_orchestrator(tmp_path=None, **kwargs) -> BuildOrchestrator:
    defaults = dict(
        arch="amd64",
        mode="mock",
        distro="debian-12",
        init_system="systemd",
        desktop=None,
        output_format="iso",
    )
    defaults.update(kwargs)
    orch = BuildOrchestrator(**defaults)
    if tmp_path:
        orch.workdir = tmp_path / orch.arch
        orch.target_root = orch.workdir / "chroot"
    return orch

class TestOrchestrator:
    def test_construction(self):
        orch = make_orchestrator()
        assert orch.arch == "amd64"
        assert orch.distro == "debian-12"

    def test_validate(self):
        orch = make_orchestrator()
        report = orch.validate()
        assert report.get("valid") is True

    def test_bootloader_profile_is_explicit_in_config(self):
        orch = make_orchestrator(bootloader="grub2-uefi")
        assert orch.config.get("bootloader", {}).get("type") == "grub2-uefi"
        assert orch.config.get("bootloader_type") == "grub2-uefi"

    def test_cli_style_overrides_reach_runtime_config(self):
        orch = make_orchestrator(compression="xz", hostname="workstation", live_user_name="demo", with_flathub=True)
        assert orch.config["compression"] == "xz"
        assert orch.config["hostname"] == "workstation"
        assert orch.config["live_user"]["name"] == "demo"
        assert "flatpak" in orch.config["software"]

    def test_desktop_live_build_gets_complete_runtime_profiles(self):
        orch = make_orchestrator(desktop="xfce", variant="live")
        assert {
            "filesystems", "networking", "network-shares", "printing",
            "security", "system-utils", "multimedia", "firmware",
        } <= set(orch.package_profiles)
        assert "pipewire" in orch.package_profiles
        assert "desktop-utils" in orch.package_profiles
        assert "update-tools" in orch.package_profiles
        assert "synaptic" in orch.config["software"]
        assert {"vlc", "cups", "samba", "pciutils", "gvfs-backends", "gvfs-fuse"} <= set(orch.config["software"])

    def test_pulseaudio_profile_replaces_pipewire_default(self):
        orch = make_orchestrator(desktop="xfce", variant="live", package_profiles=["pulseaudio"])
        assert "pulseaudio" in orch.config["software"]
        assert "pipewire" not in orch.config["software"]

    def test_non_live_build_keeps_optional_desktop_profiles_out(self):
        orch = make_orchestrator(desktop="xfce", variant="minimal")
        assert "network-shares" not in orch.package_profiles
        assert "samba" not in orch.config["software"]

    def test_kde_uses_its_native_update_frontend(self):
        orch = make_orchestrator(desktop="kde", variant="live")
        assert "update-tools" not in orch.package_profiles

    def test_non_graphical_build_does_not_install_synaptic(self):
        orch = make_orchestrator(variant="live")
        assert "desktop-utils" not in orch.package_profiles
        assert "synaptic" not in orch.config["software"]

    def test_mock_build_debian(self, tmp_path):
        orch = make_orchestrator(tmp_path=tmp_path, distro="debian-12", desktop="gnome")
        result = orch.build()
        assert isinstance(result, Path)

    def test_mock_build_devuan(self, tmp_path):
        orch = make_orchestrator(tmp_path=tmp_path, distro="devuan-5", init_system="openrc", desktop="xfce")
        result = orch.build()
        assert isinstance(result, Path)

    def test_devuan_default_init_does_not_pull_systemd(self):
        orch = make_orchestrator(distro="devuan-5", init_system=None)
        assert orch.init_system == "sysvinit"
        assert "systemd-sysv" not in orch.config["software"]
        assert "live-config-sysvinit" in orch.config["software"]

    def test_nonbootable_artifacts_do_not_include_firmware(self):
        orch = make_orchestrator(output_format="oci")
        assert "firmware-iwlwifi" not in orch.config["software"]
        assert "dosfstools" not in orch.config["software"]

    def test_hyprland_rejects_unsupported_bookworm(self):
        orch = make_orchestrator(distro="debian-12", desktop="hyprland")
        report = orch.validate()
        assert report["valid"] is False
        assert "supported suites" in report["errors"][0]

    @pytest.mark.parametrize("output_format,extension", [
        ("iso", ".iso"), ("img", ".img"), ("raw", ".raw"),
        ("qcow2", ".qcow2"), ("vdi", ".vdi"), ("vmdk", ".vmdk"),
        ("vhd", ".vhd"), ("vhdx", ".vhdx"), ("tarball", ".tar.xz"),
        ("oci", ".oci.tar"), ("container", ".oci.tar"),
    ])
    def test_mock_build_artifact_matrix(self, tmp_path, output_format, extension):
        orch = make_orchestrator(tmp_path=tmp_path, output_format=output_format)
        result = orch.build(output_name=str(tmp_path / "artifacts" / "test.iso"))
        assert str(result).endswith(extension)
        assert result.exists()

    def test_mock_netboot_artifact(self, tmp_path):
        orch = make_orchestrator(
            tmp_path=tmp_path,
            output_format="netboot",
            with_debian_installer=True,
            di_mode="netboot",
        )
        result = orch.build(output_name=str(tmp_path / "artifacts" / "debian"))
        assert result.name.endswith(".netboot.tar.gz")
        assert result.exists()

    def test_installer_combinations_are_validated(self):
        assert make_orchestrator(with_calamares=True).validate()["valid"] is False
        assert make_orchestrator(
            distro="devuan-5", init_system="sysvinit", with_debian_installer=True
        ).validate()["valid"] is False
        assert make_orchestrator(
            with_debian_installer=True, di_mode="netinstall", output_format="iso"
        ).validate()["valid"] is True

    def test_hardware_profile_selects_platform_bootloader(self):
        orch = make_orchestrator(
            arch="aarch64",
            distro="debian-13",
            output_format="img",
            hardware_profile="pinebookpro",
            bootloader=None,
        )
        assert orch.config["bootloader_type"] == "u-boot-pinebookpro"

    def test_vm_profile_requires_its_native_format(self):
        valid = make_orchestrator(output_format="qcow2", vm_profile="qemu")
        invalid = make_orchestrator(output_format="vdi", vm_profile="qemu")
        assert valid.validate()["valid"] is True
        assert invalid.validate()["valid"] is False

    def test_mock_build_tarball(self, tmp_path):
        orch = make_orchestrator(tmp_path=tmp_path, distro="debian-12", output_format="tarball")
        result = orch.build()
        assert isinstance(result, Path)
        assert result.name.endswith(".tar.xz")

    def test_output_inside_workdir_is_rejected_before_cleanup(self, tmp_path):
        orch = make_orchestrator(tmp_path=tmp_path)
        with pytest.raises(Exception, match="inside the disposable workdir"):
            orch.build(output_name=str(orch.workdir / "artifact.iso"))

    def test_invalid_live_user_is_rejected_before_build(self):
        orch = make_orchestrator(live_user_name="bad;user")
        assert "Live user name must be a valid Linux login name." in orch.validate()["errors"]

    def test_generated_checksums_are_verified(self, tmp_path):
        artifact = tmp_path / "artifact.iso"
        artifact.write_bytes(b"artifact contents")
        orch = make_orchestrator()
        orch._generate_checksums(artifact)
        assert (tmp_path / "artifact.iso.sha256").is_file()
        assert (tmp_path / "artifact.iso.md5").is_file()


@pytest.mark.parametrize("output_format,di_mode,fail_stage", [
    ("iso", None, None), ("img", None, None),
    ("netboot", "netboot", None), ("iso", "netinstall", None),
    ("iso", None, "pre-chroot"), ("iso", None, "chroot"),
    ("iso", None, "post-chroot"),
])
def test_three_hook_phases_follow_build_lifecycle_and_always_unmount(
    tmp_path, monkeypatch, output_format, di_mode, fail_stage,
):
    from deb_dev_builder.core import orchestrator as module
    from deb_dev_builder.core.hook_manager import HookError, HookManager
    events = []
    artifact = tmp_path / "result.bin"
    options = dict(output_format=output_format)
    if di_mode:
        options.update(with_debian_installer=True, di_mode=di_mode)
    orch = make_orchestrator(tmp_path=tmp_path, **options)
    installer_only = di_mode is not None

    for method in ("setup", "mount_virtual_fs", "umount_virtual_fs"):
        monkeypatch.setattr(module.ToolchainManager, method,
                            lambda self, method=method: events.append("toolchain:" + method))

    def mount(self):
        self.is_mounted = True
        events.append("mount")

    def unmount(self):
        self.is_mounted = False
        events.append("unmount")

    monkeypatch.setattr(module.ChrootManager, "mount_virtual_fs", mount)
    monkeypatch.setattr(module.ChrootManager, "umount_virtual_fs", unmount)
    monkeypatch.setattr(module, "unmount_all_under", lambda root: None)
    monkeypatch.setattr(module, "mountpoints_under", lambda root: [])
    for method in ("bootstrap_rootfs", "configure_sources_list", "update_apt_cache", "install_packages"):
        monkeypatch.setattr(module.APTManager, method,
                            lambda self, *args, method=method, **kwargs: events.append(method))
    monkeypatch.setattr(module.SystemCustomizer, "configure_live_environment",
                        lambda self: events.append("customize"))
    monkeypatch.setattr(module.ChrootCleaner, "clean", lambda self: {"removed": 0})

    def build_artifact(self, *args, **kwargs):
        events.append("artifact")
        artifact.write_bytes(b"artifact")
        return artifact

    monkeypatch.setattr(module.ISOEngine, "build_iso", build_artifact)
    monkeypatch.setattr(module.ISOEngine, "build_netboot_archive", build_artifact)
    monkeypatch.setattr(module.DiskEngine, "build_disk_image", build_artifact)
    monkeypatch.setattr(orch, "_generate_checksums", lambda *args, **kwargs: events.append("checksums"))
    original = HookManager.run_stage

    def phase(self, stage, **kwargs):
        assert self.hooks_base == module.resolve_from_project("configs/hooks")
        events.append(stage)
        if stage == "pre-chroot":
            assert "toolchain:setup" not in events
        elif stage == "chroot":
            assert self.chroot.is_mounted
            assert "install_packages" in events and "customize" in events
            assert "artifact" not in events
        else:
            assert not self.chroot.is_mounted
            assert kwargs["artifact"].is_file()
        if stage == fail_stage:
            raise HookError("deliberate failure")
        return original(self, stage, **kwargs)

    monkeypatch.setattr(HookManager, "run_stage", phase)
    if fail_stage:
        with pytest.raises(HookError, match="deliberate failure"):
            orch.build(output_name=str(artifact))
        assert "checksums" not in events
        if fail_stage != "post-chroot":
            assert "artifact" not in events
    else:
        assert orch.build(output_name=str(artifact)) == artifact
        expected = ["pre-chroot", "post-chroot"] if installer_only else list(HookManager.STAGES)
        assert [event for event in events if event in HookManager.STAGES] == expected
        assert events.index("post-chroot") < events.index("checksums")
    assert "unmount" in events
    assert "toolchain:umount_virtual_fs" in events
