import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, List, Union
import logging

logger = logging.getLogger("toolchain_manager")

class ToolchainManagerError(Exception):
    pass

class ToolchainManager:
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
        self.build_host_dir = self.workdir_base / "build_host"
        self.is_mounted = False

    def setup(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Simulating build_host setup.")
            return

        self.build_host_dir.mkdir(parents=True, exist_ok=True)

    def mount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Mounting virtual filesystems into build_host.")
            self.is_mounted = True
            return

        self.is_mounted = True

    def umount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK TOOLCHAIN] Unmounting virtual filesystems from build_host.")
            self.is_mounted = False
            return

        self.is_mounted = False

    def run_tool(self, tool_binary: str, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        if self.mode == "mock":
            cmd_str = f"{tool_binary} {' '.join(args)}"
            logger.info(f"[MOCK TOOL EXEC] {cmd_str}")
            return subprocess.CompletedProcess(args=[tool_binary] + args, returncode=0, stdout="", stderr="")

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
