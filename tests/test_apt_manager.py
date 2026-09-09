import pytest
from pathlib import Path
import subprocess
from deb_dev_builder.core.chroot_manager import ChrootManager
from deb_dev_builder.core.apt_manager import APTManager


class IsolatedToolchain:
    use_isolated = True

    @staticmethod
    def _tool_in_build_host(tool):
        return tool == "mmdebstrap"

class TestAPTManager:
    def test_empty_exclusions_remove_stale_builder_preferences(self, tmp_path):
        chroot = ChrootManager(tmp_path / "chroot", mode="real")
        preferences = chroot.target_root / "etc/apt/preferences.d/99deb-dev-builder-excludes"
        preferences.parent.mkdir(parents=True)
        preferences.write_text("Package: desktop-base\nPin: version *\nPin-Priority: -1\n")
        APTManager(chroot, {"exclude_packages": []}).install_packages([])
        assert not preferences.exists()

    def test_real_chroot_unmounts_recursive_virtual_mounts(self, tmp_path, monkeypatch):
        root = tmp_path / "chroot"
        for name in ("dev", "sys", "proc"):
            (root / name).mkdir(parents=True)
        chroot = ChrootManager(root, mode="real")
        chroot.is_mounted = True
        unmounted = []

        monkeypatch.setattr(
            "deb_dev_builder.core.chroot_manager.unmount_all_under",
            lambda path: unmounted.append(path),
        )
        monkeypatch.setattr(
            "deb_dev_builder.core.chroot_manager.subprocess.run",
            lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
        )

        chroot.umount_virtual_fs()

        assert unmounted == [root / "dev", root / "sys", root / "proc"]
        assert chroot.is_mounted is False

    def test_default_cache_is_kept_outside_the_workspace(self, tmp_path):
        target_root = tmp_path / "workdir" / "amd64" / "chroot"
        chroot = ChrootManager(target_root, mode="mock", arch="amd64")
        cache_dir = APTManager(chroot, config={"base_distro": "debian", "suite": "trixie"}).resolve_cache_dir()
        assert cache_dir == tmp_path / "cache" / "debian" / "trixie" / "amd64" / "apt"

    def test_cache_isolated_between_debian_and_devuan(self, tmp_path):
        target_root = tmp_path / "workdir" / "amd64" / "chroot"
        chroot = ChrootManager(target_root, mode="mock", arch="amd64")
        debian = APTManager(chroot, {"base_distro": "debian", "suite": "trixie"}).resolve_cache_dir()
        devuan = APTManager(chroot, {"base_distro": "devuan", "suite": "daedalus"}).resolve_cache_dir()
        assert debian != devuan

    def test_mock_bootstrap(self, tmp_path):
        target_root = tmp_path / "chroot"
        chroot = ChrootManager(target_root, mode="mock")
        apt = APTManager(chroot, config={"suite": "bookworm", "architecture": "amd64"})
        apt.bootstrap_rootfs("bookworm", "amd64")
        assert target_root.exists()

    def test_mmdebstrap_receives_an_empty_target_directory(self, tmp_path, monkeypatch):
        target_root = tmp_path / "chroot"
        chroot = ChrootManager(target_root, mode="real", arch="amd64")
        apt = APTManager(chroot, config={"distro": "debian-13", "suite": "trixie", "mirror": "http://example.invalid", "components": ["main"]})
        seen = []

        monkeypatch.setattr("deb_dev_builder.core.apt_manager.shutil.which", lambda tool: "/usr/bin/mmdebstrap" if tool == "mmdebstrap" else None)

        def fake_run(command, **kwargs):
            if command[0] == "mmdebstrap":
                seen.append(list(target_root.iterdir()) if target_root.exists() else [])
                target_root.mkdir(parents=True, exist_ok=True)
                (target_root / "etc" / "apt").mkdir(parents=True, exist_ok=True)
                (target_root / "etc" / "os-release").write_text("ID=debian\n")
                status = target_root / "var" / "lib" / "dpkg" / "status"
                status.parent.mkdir(parents=True, exist_ok=True)
                status.write_text("Package: base-files\nStatus: install ok installed\n\n")
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr("deb_dev_builder.core.apt_manager.subprocess.run", fake_run)
        monkeypatch.setattr(apt, "sync_cache_from_target", lambda: None)
        apt.bootstrap_rootfs("trixie", "amd64", use_seed=False)

        assert seen == [[]]
        # Desktop roots must retain package recommendations; only language
        # downloads are optimized away later in the build.
        assert "--aptopt=APT::Install-Recommends=true" in getattr(apt, "_last_bootstrap_command", [])

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
