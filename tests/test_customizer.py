import pytest
import subprocess
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

def test_theme_mapping_targets_the_real_chroot_attribute(tmp_path):
    root = tmp_path / "chroot"
    chroot = ChrootManager(target_root=root, mode="real")
    SystemCustomizer(chroot=chroot, config={}).apply_theme_assets(chroot)
    assert (root / "usr" / "share" / "backgrounds" / "default-wallpaper.png").is_file()

def test_calamares_creates_launcher_and_polkit_rule(tmp_path):
    root = tmp_path / "chroot"
    chroot = ChrootManager(target_root=root, mode="real")
    customizer = SystemCustomizer(chroot=chroot, config={"with_calamares": True})
    customizer.configure_calamares()
    assert (root / "usr" / "share" / "applications" / "install-system.desktop").is_file()
    assert (root / "etc" / "polkit-1" / "rules.d" / "49-calamares.rules").is_file()


def test_live_user_password_is_passed_to_chpasswd_without_a_shell(tmp_path):
    class RecordingChroot:
        mode = "real"

        def __init__(self, root):
            self.target_root = root
            self.calls = []

        def run_in_chroot(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0)

    chroot = RecordingChroot(tmp_path / "chroot")
    SystemCustomizer(chroot, {"live_user": {"name": "builder", "password": "safe-password"}}).setup_live_users()

    password_calls = [(command, kwargs) for command, kwargs in chroot.calls if command == ["chpasswd"]]
    assert password_calls == [(["chpasswd"], {"check": False, "input_data": "builder:safe-password\n", "text": True})]
    assert not any(command == ["passwd", "-u", "root"] for command, _ in chroot.calls)


def test_live_user_name_rejects_shell_metacharacters(tmp_path):
    chroot = ChrootManager(target_root=tmp_path / "chroot", mode="real")
    customizer = SystemCustomizer(chroot, {"live_user": {"name": "bad'; touch /owned #"}})
    with pytest.raises(ValueError, match="Invalid live user name"):
        customizer.setup_live_users()
