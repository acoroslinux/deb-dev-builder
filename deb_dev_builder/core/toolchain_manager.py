import os
import platform
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Dict, Any, Optional, List, Union
import logging

logger = logging.getLogger("toolchain_manager")

_HOST_ARCH = platform.machine().lower()

class ToolchainManagerError(Exception):
    pass

class ToolchainManager:
    """
    Manages an isolated secondary chroot (build_host), containing all
    build and ISO creation tools (mmdebstrap, debootstrap, mksquashfs, grub-mkstandalone, xorriso, mtools, qemu-utils).
    This ensures deb-dev-builder is 100% host distribution agnostic.
    """

    def __init__(
        self,
        workdir_base: Path,
        mode: str = "mock",
        force_isolated: bool = False,
        target_arch: str = "amd64",
        distro: str = "debian-12",
    ):
        self.workdir_base = Path(workdir_base).resolve()
        self.mode = mode.lower()
        self.force_isolated = force_isolated
        self.target_arch = target_arch.lower()
        self.distro = distro
        self.build_host_dir = self.workdir_base.parent / "build_host"
        self.cache_dir = self.workdir_base.parent / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.is_mounted = False

        from deb_dev_builder.core.path_utils import resolve_from_project
        self.project_root = resolve_from_project(".")
        rel_proj = self.project_root.relative_to("/")
        self.project_mount = self.build_host_dir / rel_proj

    def check_host_tools(self) -> bool:
        """Check if primary ISO packaging tools exist on the host."""
        required_tools = ["mksquashfs", "xorriso", "grub-mkstandalone", "mtools", "zstd"]
        missing = [tool for tool in required_tools if shutil.which(tool) is None]
        if missing:
            logger.info(f"Missing tools on host: {', '.join(missing)}")
            return False
        return True

    def setup(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Simulating build_host setup.")
            return

        if not self.force_isolated and self.check_host_tools():
            logger.info("Host has native build tools. Using host environment directly.")
            return

        self.bootstrap_build_host()

    def _is_bootstrapped(self) -> bool:
        return (self.build_host_dir / "etc" / "os-release").exists() and (self.build_host_dir / "usr" / "bin" / "xorriso").exists()

    def bootstrap_build_host(self):
        if self._is_bootstrapped():
            logger.info(f"♻️ Reusing existing isolated build_host environment at: {self.build_host_dir}")
            return

        logger.info(f"🚀 Initializing isolated build environment (build_host) at: {self.build_host_dir}")
        self.build_host_dir.mkdir(parents=True, exist_ok=True)

        mirror = "http://deb.debian.org/debian"
        suite = "bookworm"

        host_tools = [
            "squashfs-tools", "zstd", "xorriso", "grub-common", "grub-pc-bin",
            "grub-efi-amd64-bin", "grub-efi-ia32-bin", "mtools", "dosfstools", "qemu-utils",
            "syslinux-utils", "fdisk", "util-linux", "ca-certificates"
        ]

        if shutil.which("mmdebstrap"):
            cmd = [
                "mmdebstrap",
                "--variant=essential",
                f"--include={','.join(host_tools)}",
                suite,
                str(self.build_host_dir),
                mirror,
            ]
        elif shutil.which("debootstrap"):
            cmd = [
                "debootstrap",
                f"--include={','.join(host_tools)}",
                suite,
                str(self.build_host_dir),
                mirror,
            ]
        else:
            logger.warning("Neither mmdebstrap nor debootstrap found on host system. Using host tools directly.")
            return

        res = subprocess.run(cmd, check=False)
        if res.returncode != 0:
            logger.warning(f"Build-host bootstrap failed with code {res.returncode}; falling back to host binaries.")
            return

        host_resolv = Path("/etc/resolv.conf")
        if host_resolv.exists():
            resolv_dest = self.build_host_dir / "etc" / "resolv.conf"
            resolv_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(host_resolv, resolv_dest)

        logger.info("✅ Isolated build_host environment ready!")

    def mount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Mounting virtual filesystems into build_host.")
            self.is_mounted = True
            return

        if not self.build_host_dir.exists() or not self._is_bootstrapped():
            return

        mounts = [
            ("proc", self.build_host_dir / "proc", "proc", None),
            ("sysfs", self.build_host_dir / "sys", "sysfs", None),
            ("devtmpfs", self.build_host_dir / "dev", "devtmpfs", None),
        ]
        for src, target, fstype, opts in mounts:
            target.mkdir(parents=True, exist_ok=True)
            cmd = ["mount", "-t", fstype]
            if opts:
                cmd.extend(["-o", opts])
            cmd.extend([src, str(target)])
            subprocess.run(cmd, check=False, stderr=subprocess.DEVNULL)

        self.project_mount.mkdir(parents=True, exist_ok=True)
        subprocess.run(["mount", "--bind", str(self.project_root), str(self.project_mount)], check=False)

        self.is_mounted = True

    def umount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Unmounting virtual filesystems from build_host.")
            self.is_mounted = False
            return

        if not self.build_host_dir.exists():
            return

        for path in [
            self.project_mount,
            self.build_host_dir / "dev",
            self.build_host_dir / "sys",
            self.build_host_dir / "proc",
        ]:
            if path.exists():
                subprocess.run(["umount", "-l", str(path)], check=False, stderr=subprocess.DEVNULL)

        self.is_mounted = False

    def run_tool(self, tool_binary: str, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        if self.mode == "mock":
            cmd_str = f"{tool_binary} {' '.join(args)}"
            logger.info(f"[MOCK TOOL EXEC] {cmd_str}")
            return subprocess.CompletedProcess(args=[tool_binary] + args, returncode=0, stdout="", stderr="")

        if self.is_mounted and (self.build_host_dir / "usr" / "bin" / tool_binary).exists():
            cmd = ["chroot", str(self.build_host_dir), tool_binary] + args
        else:
            cmd = [tool_binary] + args

        return subprocess.run(cmd, check=check)

    def run_in_build_host(self, command: Union[str, List[str]], check: bool = True) -> subprocess.CompletedProcess:
        if self.mode == "mock":
            cmd_str = command if isinstance(command, str) else " ".join(command)
            logger.info(f"[MOCK BUILD_HOST EXEC] {cmd_str}")
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

        if isinstance(command, str):
            cmd = ["chroot", str(self.build_host_dir), "/bin/sh", "-c", command]
        else:
            cmd = ["chroot", str(self.build_host_dir)] + command

        return subprocess.run(cmd, check=check)
