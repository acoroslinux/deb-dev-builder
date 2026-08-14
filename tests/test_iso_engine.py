import pytest
from pathlib import Path
from deb_dev_builder.core.toolchain_manager import ToolchainManager
from deb_dev_builder.core.iso_engine import ISOEngine

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
