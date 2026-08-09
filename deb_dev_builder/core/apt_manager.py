import os
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
        cache_path_str = self.config.get("system", {}).get("apt_cache", "workdir/cache/apt")
        candidate = Path(cache_path_str)
        if not candidate.is_absolute():
            from deb_dev_builder.core.path_utils import resolve_from_project
            candidate = resolve_from_project(candidate) / arch

        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            import tempfile
            fallback = Path(tempfile.gettempdir()) / "deb-dev-builder-cache" / "apt" / arch
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def configure_sources_list(self):
        sources_dir = self.target_root / "etc" / "apt"
        sources_dir.mkdir(parents=True, exist_ok=True)
        if self.chroot.mode == "mock":
            (sources_dir / "sources.list").touch()
            return

        mirror = self.config.get("mirror", "http://deb.debian.org/debian")
        suite = self.config.get("suite", "bookworm")
        components = self.config.get("components", ["main", "contrib", "non-free-firmware"])
        comp_str = " ".join(components)

        sources_content = f"deb {mirror} {suite} {comp_str}\n"
        if "security_mirror" in self.config:
            sec_mirror = self.config["security_mirror"]
            sources_content += f"deb {sec_mirror} {suite}-security {comp_str}\n"
        if "updates_mirror" in self.config:
            up_mirror = self.config["updates_mirror"]
            sources_content += f"deb {up_mirror} {suite}-updates {comp_str}\n"

        with open(sources_dir / "sources.list", "w") as f:
            f.write(sources_content)

    def bootstrap_rootfs(self, suite: str, arch: str, use_seed: bool = True):
        if self.chroot.mode == "mock":
            try:
                self.target_root.mkdir(parents=True, exist_ok=True)
                for d in ["etc/apt", "boot", "usr/bin", "var/cache/apt"]:
                    (self.target_root / d).mkdir(parents=True, exist_ok=True)
            except PermissionError:
                logger.debug("Mock rootfs creation ignored due to permissions.")
            return

        distro = self.config.get("distro", "debian-12")
        seed_cache = self.resolve_cache_dir() / f"seed-{distro}-{arch}.tar.xz"

        if use_seed and seed_cache.exists():
            logger.info(f"⚡ Fast-bootstrapping rootfs from local seed tarball: {seed_cache}")
            self.target_root.mkdir(parents=True, exist_ok=True)
            res = subprocess.run(["tar", "xpf", str(seed_cache), "-C", str(self.target_root), "--numeric-owner"])
            if res.returncode == 0:
                logger.info("Successfully bootstrapped rootfs from local seed tarball in seconds!")
                return
            else:
                logger.warning("Local seed tarball extraction failed. Falling back to network bootstrap.")

        mirror = self.config.get("mirror", "http://deb.debian.org/debian")
        components = ",".join(self.config.get("components", ["main", "contrib", "non-free-firmware"]))

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
                suite,
                str(self.target_root),
                mirror,
            ]

        res = subprocess.run(cmd)
        if res.returncode != 0:
            raise APTManagerError(f"Bootstrap failed with exit code: {res.returncode}")

        # Save seed tarball for future instant builds (excluding virtual kernel filesystems)
        try:
            logger.info(f"Caching rootfs seed tarball to {seed_cache}...")
            subprocess.run([
                "tar", "cJpf", str(seed_cache),
                "--exclude=./proc/*", "--exclude=./sys/*", "--exclude=./dev/*", "--exclude=./tmp/*", "--exclude=./run/*",
                "-C", str(self.target_root), "."
            ], check=False)
        except Exception as e:
            logger.warning(f"Could not save seed tarball cache: {e}")

    def update_apt_cache(self):
        if self.chroot.mode == "mock":
            return
        self.chroot.run_in_chroot(["apt-get", "update", "-y"])

    def install_packages(self, packages: List[str]):
        if not packages or self.chroot.mode == "mock":
            return
        real_pkgs = [p for p in packages if p]
        if not real_pkgs:
            return
        cmd = ["apt-get", "install", "-y", "--no-install-recommends"] + real_pkgs
        res = self.chroot.run_in_chroot(cmd, check=False)
        if res.returncode != 0:
            logger.warning(f"APT package installation returned code {res.returncode}")

    def clean_cache(self):
        if self.chroot.mode == "mock":
            return
        self.chroot.run_in_chroot(["apt-get", "clean"])
