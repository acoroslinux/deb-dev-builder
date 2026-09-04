import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, List, Union
import logging

logger = logging.getLogger("chroot_manager")

class ChrootManagerError(Exception):
    """Exception raised for errors in ChrootManager."""
    pass

class ChrootManager:
    def __init__(self, target_root: Path, mode: str = "mock", cache_dir: Optional[Path] = None, arch: str = "amd64"):
        self.target_root = Path(target_root).resolve()
        self.mode = mode.lower()
        self.cache_dir = Path(cache_dir).resolve() if cache_dir else None
        self.arch = arch.lower()
        self.virtual_mounts = ["proc", "sys", "dev", "dev/pts"]
        self._policy_rc_backup = None
        self._policy_rc_active = False
        self.is_mounted = False

    def mount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK CHROOT] Simulating mounting virtual filesystems.")
            self.is_mounted = True
            return

        self.target_root.mkdir(parents=True, exist_ok=True)
        self.is_mounted = True
        mounts = [
            ("proc", self.target_root / "proc", "proc", None),
            ("sysfs", self.target_root / "sys", "sysfs", None),
            ("/dev", self.target_root / "dev", None, "--rbind"),
        ]
        for src, target, fstype, opts in mounts:
            target.mkdir(parents=True, exist_ok=True)
            if opts == "--rbind":
                cmd = ["mount", "--rbind", src, str(target)]
                result = subprocess.run(cmd, check=False, stderr=subprocess.PIPE, text=True)
                if result.returncode == 0:
                    subprocess.run(["mount", "--make-rslave", str(target)], check=False)
                else:
                    raise ChrootManagerError(f"Could not bind-mount {src} at {target}: {result.stderr.strip()}")
                continue
            cmd = ["mount", "-t", fstype]
            cmd.extend([src, str(target)])
            result = subprocess.run(cmd, check=False, stderr=subprocess.PIPE, text=True)
            if result.returncode != 0:
                raise ChrootManagerError(f"Could not mount {fstype} at {target}: {result.stderr.strip()}")

        policy_file = self.target_root / "usr" / "sbin" / "policy-rc.d"
        policy_file.parent.mkdir(parents=True, exist_ok=True)
        if policy_file.exists():
            self._policy_rc_backup = (policy_file.read_bytes(), policy_file.stat().st_mode & 0o777)
        policy_file.write_text("#!/bin/sh\nexit 101\n")
        policy_file.chmod(0o755)
        self._policy_rc_active = True

    def umount_virtual_fs(self):
        if self.mode == "mock":
            logger.info("[MOCK CHROOT] Simulating unmounting virtual filesystems.")
            self.is_mounted = False
            return
        if not self.is_mounted:
            return

        policy_file = self.target_root / "usr" / "sbin" / "policy-rc.d"
        if self._policy_rc_active and self._policy_rc_backup is not None:
            content, mode = self._policy_rc_backup
            policy_file.write_bytes(content)
            policy_file.chmod(mode)
            self._policy_rc_backup = None
        elif self._policy_rc_active and policy_file.exists():
            policy_file.unlink()
        self._policy_rc_active = False

        for path in [
            self.target_root / "dev",
            self.target_root / "sys",
            self.target_root / "proc",
        ]:
            if path.exists():
                subprocess.run(["umount", "-l", str(path)], check=False, stderr=subprocess.DEVNULL)
        self.is_mounted = False

    def run_in_chroot(
        self,
        command: Union[str, List[str]],
        check: bool = True,
        env: Optional[dict] = None,
        capture_output: bool = False,
        text: bool = False,
        input_data: Optional[str | bytes] = None,
    ) -> subprocess.CompletedProcess:
        if self.mode == "mock":
            cmd_str = command if isinstance(command, str) else " ".join(command)
            logger.info(f"[MOCK CHROOT EXEC] {cmd_str}")
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

        if isinstance(command, str):
            cmd = ["chroot", str(self.target_root), "/bin/sh", "-c", command]
        else:
            cmd = ["chroot", str(self.target_root)] + command

        full_env = os.environ.copy()
        full_env["DEBIAN_FRONTEND"] = "noninteractive"
        full_env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        if env:
            full_env.update(env)

        return subprocess.run(
            cmd,
            check=check,
            env=full_env,
            capture_output=capture_output,
            text=text,
            input=input_data,
        )
