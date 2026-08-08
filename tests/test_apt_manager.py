import pytest
from pathlib import Path
from deb_dev_builder.core.chroot_manager import ChrootManager
from deb_dev_builder.core.apt_manager import APTManager

class TestAPTManager:
    def test_mock_bootstrap(self, tmp_path):
        target_root = tmp_path / "chroot"
        chroot = ChrootManager(target_root, mode="mock")
        apt = APTManager(chroot, config={"suite": "bookworm", "architecture": "amd64"})
        apt.bootstrap_rootfs("bookworm", "amd64")
        assert target_root.exists()
