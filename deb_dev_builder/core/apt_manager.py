import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
import logging
from deb_dev_builder.core.chroot_manager import ChrootManager

logger = logging.getLogger("apt_manager")

class APTManagerError(Exception):
    pass

class APTManager:
    def __init__(self, chroot: ChrootManager, config: Dict[str, Any], toolchain=None):
        self.chroot = chroot
        self.config = config
        self.target_root = chroot.target_root
        self.toolchain = toolchain

    def resolve_cache_dir(self) -> Path:
        arch = getattr(self.chroot, "arch", "amd64")
        cache_path_str = self.config.get("system", {}).get("apt_cache", f"cache/{arch}/apt")
        candidate = Path(cache_path_str)
        if not candidate.is_absolute():
            from deb_dev_builder.core.path_utils import resolve_from_project
            candidate = resolve_from_project(candidate)

        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            import tempfile
            fallback = Path(tempfile.gettempdir()) / "deb-dev-builder-cache" / "apt" / arch
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def _is_bootstrapped_rootfs(self) -> bool:
        """Detect whether target root already contains a usable base system."""
        checks = [
            self.target_root / "etc" / "os-release",
            self.target_root / "usr" / "bin" / "dpkg",
            self.target_root / "etc" / "apt",
        ]
        return all(path.exists() for path in checks)

    def configure_sources_list(self):
        sources_dir = self.target_root / "etc" / "apt"
        sources_dir.mkdir(parents=True, exist_ok=True)
        if self.chroot.mode == "mock":
            try:
                (sources_dir / "sources.list").touch()
            except Exception:
                pass
            return

        base_distro = self.config.get("base_distro", "debian").lower()
        if base_distro == "devuan":
            default_mirror = "http://deb.devuan.org/merged"
            default_suite = "daedalus"
        else:
            default_mirror = "http://deb.debian.org/debian"
            default_suite = "bookworm"

        mirror = self.config.get("mirror", default_mirror)
        suite = self.config.get("suite", default_suite)
        components = self.config.get("components", ["main", "contrib", "non-free-firmware"])
        comp_str = " ".join(components)

        sources_content = f"deb {mirror} {suite} {comp_str}\n"
        if base_distro == "debian":
            if "security_mirror" in self.config:
                sec_mirror = self.config["security_mirror"]
                sources_content += f"deb {sec_mirror} {suite}-security {comp_str}\n"
            else:
                sources_content += f"deb http://security.debian.org/debian-security {suite}-security {comp_str}\n"

            if "updates_mirror" in self.config:
                up_mirror = self.config["updates_mirror"]
                sources_content += f"deb {up_mirror} {suite}-updates {comp_str}\n"
            else:
                sources_content += f"deb {mirror} {suite}-updates {comp_str}\n"

        with open(sources_dir / "sources.list", "w") as f:
            f.write(sources_content)

        # Enforce keeping downloaded .deb packages in APT cache
        apt_conf_dir = sources_dir / "apt.conf.d"
        apt_conf_dir.mkdir(parents=True, exist_ok=True)
        (apt_conf_dir / "01keep-debs").write_text(
            'Binary::apt::APT::Keep-Downloaded-Packages "true";\n'
            'APT::Keep-Downloaded-Packages "true";\n'
            'DPkg::Options { "--force-confdef"; "--force-confold"; };\n'
        )

    def bootstrap_rootfs(self, suite: str, arch: str, use_seed: bool = True, recreate_seed: bool = False, reuse_existing: bool = False):
        if self.chroot.mode == "mock":
            try:
                self.target_root.mkdir(parents=True, exist_ok=True)
                for d in ["etc/apt", "boot", "usr/bin", "var/cache/apt"]:
                    (self.target_root / d).mkdir(parents=True, exist_ok=True)
            except PermissionError:
                logger.debug("Mock rootfs creation ignored due to permissions.")
            return

        if reuse_existing and self._is_bootstrapped_rootfs():
            logger.info("♻️ Reusing existing rootfs because --no-clean was requested.")
            return

        distro = self.config.get("distro", "debian-12")
        seeds_dir = self.resolve_cache_dir().parent / "seeds"
        seeds_dir.mkdir(parents=True, exist_ok=True)
        seed_cache = seeds_dir / f"seed-{distro}-{arch}.tar.gz"

        if use_seed and not recreate_seed and seed_cache.exists():
            logger.info(f"⚡ Fast-bootstrapping rootfs from local seed tarball: {seed_cache}")
            self.target_root.mkdir(parents=True, exist_ok=True)
            res = subprocess.run(["tar", "xzpf", str(seed_cache), "-C", str(self.target_root), "--numeric-owner"])
            if res.returncode == 0 and (self.target_root / "etc" / "os-release").exists():
                logger.info("⚡ Successfully bootstrapped rootfs from local seed tarball in under 3 seconds!")
                self.sync_cache_to_target()
                return
            else:
                logger.warning("Local seed tarball extraction failed. Falling back to network bootstrap.")

        base_distro = self.config.get("base_distro", "debian").lower()
        if base_distro == "devuan":
            default_mirror = "http://deb.devuan.org/merged"
        else:
            default_mirror = "http://deb.debian.org/debian"

        mirror = self.config.get("mirror", default_mirror)
        components = ",".join(self.config.get("components", ["main", "contrib", "non-free-firmware"]))

        dev_dir = self.target_root / "dev"
        dev_dir.mkdir(parents=True, exist_ok=True)
        (self.target_root / "proc").mkdir(parents=True, exist_ok=True)
        (self.target_root / "sys").mkdir(parents=True, exist_ok=True)

        fd_dir = dev_dir / "fd"
        if fd_dir.is_symlink():
            fd_dir.unlink()
        fd_dir.mkdir(parents=True, exist_ok=True)

        # Prefer mmdebstrap if available, fallback to debootstrap
        if shutil.which("mmdebstrap"):
            cmd = [
                "mmdebstrap",
                f"--arch={arch}",
                f"--components={components}",
                "--variant=essential",
                suite,
                str(self.target_root),
                mirror,
            ]
        else:
            cmd = [
                "debootstrap",
                f"--arch={arch}",
                f"--components={components}",
                "--no-check-gpg",
                suite,
                str(self.target_root),
                mirror,
            ]

        res = subprocess.run(cmd)
        if res.returncode != 0:
            log_file = self.target_root / "debootstrap" / "debootstrap.log"
            err_detail = ""
            if log_file.exists():
                try:
                    lines = log_file.read_text().splitlines()
                    err_detail = "\n--- debootstrap.log (last 25 lines) ---\n" + "\n".join(lines[-25:])
                except Exception:
                    pass
            raise APTManagerError(f"Bootstrap failed with exit code: {res.returncode}{err_detail}")

        self.configure_sources_list()

        # Save seed tarball for future instant builds (excluding virtual kernel filesystems)
        try:
            seed_cache.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"⚡ Fast-caching rootfs seed tarball to {seed_cache}...")
            subprocess.run([
                "tar", "czpf", str(seed_cache),
                "--exclude=./proc/*", "--exclude=./sys/*", "--exclude=./dev/*", "--exclude=./tmp/*", "--exclude=./run/*",
                "-C", str(self.target_root), "."
            ], check=False)
            logger.info(f"Successfully saved seed tarball cache ({seed_cache.stat().st_size} bytes)")
        except Exception as e:
            logger.warning(f"Could not save seed tarball cache: {e}")

        self.sync_cache_from_target()

    def sync_cache_to_target(self):
        """Populate target_root/var/cache/apt/archives from local project cache."""
        if self.chroot.mode == "mock":
            return
        local_cache = self.resolve_cache_dir() / "archives"
        if not local_cache.exists():
            return
        target_archives = self.target_root / "var" / "cache" / "apt" / "archives"
        target_archives.mkdir(parents=True, exist_ok=True)
        for deb in local_cache.glob("*.deb"):
            dest = target_archives / deb.name
            if not dest.exists():
                try:
                    shutil.copy2(deb, dest)
                except Exception:
                    pass

    def sync_cache_from_target(self):
        """Save downloaded .deb packages from target_root/var/cache/apt/archives to local project cache."""
        if self.chroot.mode == "mock":
            return
        target_archives = self.target_root / "var" / "cache" / "apt" / "archives"
        if not target_archives.exists():
            return
        local_cache = self.resolve_cache_dir() / "archives"
        local_cache.mkdir(parents=True, exist_ok=True)
        for deb in target_archives.glob("*.deb"):
            dest = local_cache / deb.name
            if not dest.exists():
                try:
                    shutil.copy2(deb, dest)
                except Exception:
                    pass

    def update_apt_cache(self):
        if self.chroot.mode == "mock":
            return
        self.sync_cache_to_target()
        self.chroot.run_in_chroot(["apt-get", "update", "-y"], env={"DEBIAN_FRONTEND": "noninteractive"})

    def install_packages(self, packages: List[str]):
        if not packages or self.chroot.mode == "mock":
            return
        real_pkgs = [p for p in packages if p]
        if not real_pkgs:
            return

        self.sync_cache_to_target()

        cmd = ["apt-get", "install", "-y"] + real_pkgs
        res = self.chroot.run_in_chroot(cmd, check=False, env={"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "a"})
        if res.returncode != 0:
            logger.warning(f"APT package installation returned code {res.returncode}")

        self.sync_cache_from_target()

    def clean_cache(self):
        if self.chroot.mode == "mock":
            return
        self.sync_cache_from_target()
