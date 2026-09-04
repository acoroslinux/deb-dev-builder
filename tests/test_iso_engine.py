import pytest
import tarfile
from pathlib import Path
from deb_dev_builder.core.toolchain_manager import ToolchainManager
from deb_dev_builder.core.iso_engine import ISOEngine
from deb_dev_builder.core.path_utils import resolve_from_project, resolve_output_path

class TestISOEngine:
    def test_mock_iso_build(self, tmp_path):
        workdir = tmp_path / "amd64"
        target_root = workdir / "chroot"
        toolchain = ToolchainManager(workdir, mode="mock")
        engine = ISOEngine(workdir, target_root, "test-deb-dev", {"architecture": "amd64"}, mode="mock", toolchain=toolchain)
        iso_path = engine.build_iso()
        assert isinstance(iso_path, Path)
        assert iso_path.name.endswith(".iso")

    def test_bootloader_profile_uses_efi_only(self, tmp_path):
        workdir = tmp_path / "amd64"
        target_root = workdir / "chroot"
        toolchain = ToolchainManager(workdir, mode="mock")
        engine = ISOEngine(
            workdir,
            target_root,
            "efi-only",
            {"architecture": "amd64", "bootloader": {"type": "grub2-uefi"}},
            mode="mock",
            toolchain=toolchain,
        )

        assert engine.get_bootloader_type() == "grub2-uefi"
        assert engine.should_use_grub_efi() is True
        assert engine.should_use_grub_bios() is False
        assert engine.should_use_syslinux() is False

    def test_bootloader_profile_defaults_to_hybrid(self, tmp_path):
        workdir = tmp_path / "amd64"
        target_root = workdir / "chroot"
        toolchain = ToolchainManager(workdir, mode="mock")
        engine = ISOEngine(workdir, target_root, "hybrid", {"architecture": "amd64"}, mode="mock", toolchain=toolchain)

        assert engine.get_bootloader_type() == "grub2-hybrid"
        assert engine.should_use_grub_efi() is True
        assert engine.should_use_grub_bios() is True
        assert engine.should_use_syslinux() is False

    def test_absolute_output_replaces_an_existing_artifact_suffix(self, tmp_path):
        workdir = tmp_path / "work"
        engine = ISOEngine(workdir, workdir / "root", str(tmp_path / "custom.img"), {"architecture": "amd64"}, "mock", ToolchainManager(workdir, mode="mock"))
        assert engine.build_iso() == tmp_path / "custom.iso"

    def test_arm_does_not_attempt_legacy_bios_boot(self, tmp_path):
        workdir = tmp_path / "arm"
        engine = ISOEngine(workdir, workdir / "root", "arm", {"architecture": "aarch64", "bootloader": {"type": "grub2-hybrid"}}, "mock", ToolchainManager(workdir, mode="mock"))
        assert engine.should_use_grub_efi() is True
        assert engine.should_use_grub_bios() is False

    def test_mock_netinstall_stages_real_installer_layout_without_live_rootfs(self, tmp_path):
        workdir = tmp_path / "work"
        config = {
            "architecture": "amd64",
            "dpkg_arch": "amd64",
            "base_distro": "debian",
            "suite": "trixie",
            "with_debian_installer": True,
            "di_mode": "netinstall",
        }
        engine = ISOEngine(workdir, workdir / "root", "netinst", config, "mock", ToolchainManager(workdir, mode="mock"))
        result = engine.build_iso()
        assert result.name == "netinst.iso"
        assert (engine.iso_staging / "install" / "vmlinuz").exists()
        assert (engine.iso_staging / "install" / "initrd.gz").exists()
        assert not (engine.iso_staging / "live").exists()

    def test_real_netboot_repackages_official_tree_with_preseed(self, tmp_path, monkeypatch):
        workdir = tmp_path / "work"
        workdir.mkdir()
        config = {
            "architecture": "amd64",
            "dpkg_arch": "amd64",
            "base_distro": "debian",
            "suite": "trixie",
            "preseed": "server",
        }
        engine = ISOEngine(workdir, workdir / "root", str(tmp_path / "net"), config, "real", ToolchainManager(workdir, mode="mock"))

        def fake_download(relative, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = tmp_path / "payload"
            payload.mkdir(exist_ok=True)
            (payload / "pxelinux.0").write_bytes(b"PXE")
            with tarfile.open(destination, "w:gz") as archive:
                archive.add(payload / "pxelinux.0", arcname="pxelinux.0")

        monkeypatch.setattr(engine, "_download_installer_file", fake_download)
        result = engine.build_netboot_archive()
        with tarfile.open(result, "r:gz") as archive:
            assert {"pxelinux.0", "preseed.cfg"} <= set(archive.getnames())

    def test_real_tarball_is_readable_and_excludes_virtual_contents(self, tmp_path):
        workdir = tmp_path / "work"
        root = workdir / "root"
        (root / "etc").mkdir(parents=True)
        (root / "proc").mkdir()
        (root / "etc" / "hostname").write_text("debian\n")
        (root / "proc" / "mounted-data").write_text("exclude me")
        toolchain = ToolchainManager(workdir, mode="real", required_tools=["tar", "xz"])
        engine = ISOEngine(workdir, root, str(tmp_path / "rootfs"), {"architecture": "amd64"}, "real", toolchain)
        result = engine.build_tarball()
        with tarfile.open(result, "r:xz") as archive:
            names = archive.getnames()
            assert "./etc/hostname" in names
            assert "./proc/mounted-data" not in names

    def test_output_directory_is_project_relative_from_any_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_output_path("output/custom.qcow2", ".iso") == resolve_from_project("output/custom.iso")
