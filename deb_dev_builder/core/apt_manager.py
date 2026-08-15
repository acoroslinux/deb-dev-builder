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

        # Read configuration directly from the loaded distro profile
        mirror = self.config.get("mirror", "http://deb.debian.org/debian")
        suite = self.config.get("suite", "bookworm")
        components = self.config.get("components", ["main", "contrib", "non-free-firmware"])
        comp_str = " ".join(components)

        sources_lines = [
            f"# Primary repository",
            f"deb {mirror} {suite} {comp_str}",
        ]

        # Security updates repository (if configured for this distro profile)
        if self.config.get("security_mirror"):
            sec_mirror = self.config["security_mirror"]
            sec_suite = self.config.get("security_suite", f"{suite}-security")
            sources_lines.append(f"deb {sec_mirror} {sec_suite} {comp_str}")

        # Updates / Proposed repository (if configured for this distro profile)
        if self.config.get("updates_mirror"):
            up_mirror = self.config["updates_mirror"]
            up_suite = self.config.get("updates_suite", f"{suite}-updates")
            sources_lines.append(f"deb {up_mirror} {up_suite} {comp_str}")

        # Backports repository (if configured for this distro profile)
        if self.config.get("backports_mirror"):
            bp_mirror = self.config["backports_mirror"]
            bp_suite = self.config.get("backports_suite", f"{suite}-backports")
            sources_lines.append(f"deb {bp_mirror} {bp_suite} {comp_str}")

        # Extra / third-party repositories
        extra_repos = self.config.get("extra_repos", [])
        if extra_repos:
            sources_lines.append("")
            sources_lines.append("# Extra repositories")
            for repo in extra_repos:
                if isinstance(repo, str):
                    sources_lines.append(repo)
                elif isinstance(repo, dict):
                    repo_url = repo.get("url", "")
                    repo_suite = repo.get("suite", suite)
                    repo_comps = " ".join(repo.get("components", components))
                    sources_lines.append(f"deb {repo_url} {repo_suite} {repo_comps}")

        sources_content = "\n".join(sources_lines) + "\n"
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

        mirror = self.config.get("mirror", "http://deb.debian.org/debian")
        suite = self.config.get("suite", suite)
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

    def download_offline_packages(self, packages: List[str], dest_dir: Path) -> Path:
        """
        Downloads the specified packages (and dependencies) into dest_dir and creates Packages.gz index.
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if self.chroot.mode == "mock":
            (dest_dir / "Packages.gz").touch()
            (dest_dir / "Release").touch()
            return dest_dir

        real_pkgs = [p for p in packages if p]
        if real_pkgs:
            logger.info(f"📦 Downloading {len(real_pkgs)} offline packages into {dest_dir}...")
            cmd = ["apt-get", "install", "-y", "--download-only", "-o", f"Dir::Cache::Archives={dest_dir}"] + real_pkgs
            self.chroot.run_in_chroot(cmd, check=False, env={"DEBIAN_FRONTEND": "noninteractive"})

            # Copy any packages from target archive cache
            target_archives = self.target_root / "var" / "cache" / "apt" / "archives"
            if target_archives.exists():
                for deb in target_archives.glob("*.deb"):
                    try:
                        dst = dest_dir / deb.name
                        if not dst.exists():
                            shutil.copy2(deb, dst)
                    except Exception:
                        pass

        self.create_repository_metadata(dest_dir)
        return dest_dir

    def create_repository_metadata(self, repo_dir: Path):
        import gzip
        repo_dir = Path(repo_dir)
        repo_dir.mkdir(parents=True, exist_ok=True)
        if self.chroot.mode == "mock":
            (repo_dir / "Packages.gz").touch()
            (repo_dir / "Release").touch()
            return

        packages_file = repo_dir / "Packages"
        if shutil.which("dpkg-scanpackages"):
            res = subprocess.run(["dpkg-scanpackages", ".", "/dev/null"], cwd=str(repo_dir), capture_output=True, text=True, check=False)
            if res.returncode == 0:
                packages_file.write_text(res.stdout)
        elif shutil.which("apt-ftparchive"):
            res = subprocess.run(["apt-ftparchive", "packages", "."], cwd=str(repo_dir), capture_output=True, text=True, check=False)
            if res.returncode == 0:
                packages_file.write_text(res.stdout)

        if packages_file.exists():
            with open(packages_file, "rb") as f_in, gzip.open(repo_dir / "Packages.gz", "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        else:
            (repo_dir / "Packages.gz").touch()

        release_file = repo_dir / "Release"
        release_content = (
            "Archive: stable\n"
            "Component: main\n"
            "Origin: Offline-ISO\n"
            "Label: Offline ISO Repository\n"
            "Architecture: amd64\n"
        )
        release_file.write_text(release_content)

