import pytest
from pathlib import Path
from deb_dev_builder.core.chroot_manager import ChrootManager
from deb_dev_builder.core.customizer import SystemCustomizer

def test_system_customizer_mock(tmp_path):
    chroot = ChrootManager(target_root=tmp_path / "chroot", mode="mock")
    config = {
        "base_distro": "debian",
        "hostname": "deb-test",
        "live_user": "testuser",
        "display_manager": "lightdm",
        "with_zram": True,
        "with_flathub": True,
        "with_calamares": True,
    }
    customizer = SystemCustomizer(chroot=chroot, config=config)
    customizer.configure_live_environment()
    assert customizer.config["hostname"] == "deb-test"
