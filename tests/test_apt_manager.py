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

    def test_configure_sources_list_debian(self, tmp_path):
        target_root = tmp_path / "chroot_deb"
        chroot = ChrootManager(target_root, mode="real")
        config = {
            "distro": "debian-12",
            "suite": "bookworm",
            "mirror": "http://deb.debian.org/debian",
            "security_mirror": "http://security.debian.org/debian-security",
            "updates_mirror": "http://deb.debian.org/debian",
            "components": ["main", "contrib", "non-free-firmware"],
        }
        apt = APTManager(chroot, config=config)
        apt.configure_sources_list()

        sources = (target_root / "etc" / "apt" / "sources.list").read_text()
        assert "deb http://deb.debian.org/debian bookworm main contrib non-free-firmware" in sources
        assert "deb http://security.debian.org/debian-security bookworm-security main contrib non-free-firmware" in sources
        assert "deb http://deb.debian.org/debian bookworm-updates main contrib non-free-firmware" in sources

    def test_configure_sources_list_devuan(self, tmp_path):
        target_root = tmp_path / "chroot_devuan"
        chroot = ChrootManager(target_root, mode="real")
        config = {
            "distro": "devuan-5",
            "suite": "daedalus",
            "mirror": "http://deb.devuan.org/merged",
            "components": ["main", "contrib", "non-free"],
        }
        apt = APTManager(chroot, config=config)
        apt.configure_sources_list()

        sources = (target_root / "etc" / "apt" / "sources.list").read_text()
        assert "deb http://deb.devuan.org/merged daedalus main contrib non-free" in sources
        assert "security.debian.org" not in sources
        assert "updates" not in sources

    def test_configure_sources_list_sid(self, tmp_path):
        target_root = tmp_path / "chroot_sid"
        chroot = ChrootManager(target_root, mode="real")
        config = {
            "distro": "debian-sid",
            "suite": "sid",
            "mirror": "http://deb.debian.org/debian",
            "components": ["main", "contrib", "non-free-firmware"],
        }
        apt = APTManager(chroot, config=config)
        apt.configure_sources_list()

        sources = (target_root / "etc" / "apt" / "sources.list").read_text()
        assert "deb http://deb.debian.org/debian sid main contrib non-free-firmware" in sources
        assert "sid-security" not in sources

    def test_configure_sources_list_extra_repos(self, tmp_path):
        target_root = tmp_path / "chroot_extra"
        chroot = ChrootManager(target_root, mode="real")
        config = {
            "distro": "debian-12",
            "suite": "bookworm",
            "mirror": "http://deb.debian.org/debian",
            "backports_mirror": "http://deb.debian.org/debian",
            "components": ["main"],
            "extra_repos": [
                "deb http://packages.custom.org/repo bookworm main",
                {"url": "http://fasttrack.debian.net/debian", "suite": "bookworm-fasttrack", "components": ["main"]}
            ]
        }
        apt = APTManager(chroot, config=config)
        apt.configure_sources_list()

        sources = (target_root / "etc" / "apt" / "sources.list").read_text()
        assert "bookworm-backports" in sources
        assert "deb http://packages.custom.org/repo bookworm main" in sources
        assert "deb http://fasttrack.debian.net/debian bookworm-fasttrack main" in sources

    def test_download_offline_packages_creates_packages_gz(self, tmp_path):
        target_root = tmp_path / "chroot"
        chroot = ChrootManager(target_root, mode="mock", arch="amd64")
        apt = APTManager(chroot, config={})
        dest_dir = tmp_path / "offline_repo"

        result = apt.download_offline_packages(["gparted", "git"], dest_dir)
        assert result.exists()
        assert (dest_dir / "Packages.gz").exists()
        assert (dest_dir / "Release").exists()

