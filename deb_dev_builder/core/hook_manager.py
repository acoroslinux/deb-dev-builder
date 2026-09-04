import os
import stat
import sys
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

from deb_dev_builder.core.path_utils import resolve_from_project

logger = logging.getLogger("hook_manager")


class HookManager:
    """
    Manages and executes user-defined hook scripts across 3 canonical build phases:
      1. pre-chroot:  Runs on the host at the beginning of the build (setup / pre-chroot).
      2. chroot:      Runs inside the chroot at the end of rootfs construction, after
                      packages are installed and system configuration is completed.
      3. post-chroot: Runs on the host in the binary phase after squashfs / ISO creation.
    """

    STAGES = ["pre-chroot", "chroot", "post-chroot"]

    STAGE_ALIASES = {
        "pre_build": "pre-chroot",
        "pre_chroot": "pre-chroot",
        "pre-build": "pre-chroot",
        "post_customize": "chroot",
        "customize": "chroot",
        "post_iso": "post-chroot",
        "post_chroot": "post-chroot",
        "binary": "post-chroot",
        "post-build": "post-chroot",
        "post_build": "post-chroot",
    }

    def __init__(self, chroot_manager=None, config: Optional[Dict[str, Any]] = None):
        self.chroot = chroot_manager
        self.config = config or {}
        self.hooks_base = resolve_from_project("configs/hooks")
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Ensures the 3 standard hook directories exist."""
        for stage in self.STAGES:
            (self.hooks_base / stage).mkdir(parents=True, exist_ok=True)

    def _resolve_stage(self, stage: str) -> str:
        return self.STAGE_ALIASES.get(stage, stage)

    def run_stage(self, stage: str):
        """Executes all bash scripts in the specified hook stage directory."""
        canonical_stage = self._resolve_stage(stage)
        stage_dir = self.hooks_base / canonical_stage
        if not stage_dir.exists():
            return

        scripts = sorted([
            f for f in stage_dir.iterdir()
            if f.is_file() and not f.is_symlink() and f.suffix == ".sh"
        ])
        if not scripts:
            return

        logger.info(f"⚓ Running hooks for stage: {canonical_stage} ({len(scripts)} scripts)")

        for script in scripts:
            self._ensure_executable(script)
            logger.info(f"  -> Executing hook: {script.name}")

            if canonical_stage == "chroot":
                self._run_in_chroot(script)
            else:
                self._run_on_host(script, canonical_stage)

    def _ensure_executable(self, script: Path):
        try:
            st = script.stat()
            if not bool(st.st_mode & stat.S_IXUSR):
                script.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except Exception as e:
            logger.debug(f"Could not adjust permissions on {script}: {e}")

    def _run_on_host(self, script: Path, stage: str):
        env = os.environ.copy()
        target_root = str(getattr(self.chroot, "target_root", "")) if self.chroot else ""
        env["TARGET_ROOT"] = target_root
        env["CHROOT_PATH"] = target_root
        env["BUILD_ARCH"] = str(self.config.get("arch", ""))
        env["BUILD_DESKTOP"] = str(self.config.get("desktop", ""))
        env["HOOK_PHASE"] = stage

        try:
            subprocess.run(
                ["/bin/bash", str(script)],
                env=env,
                cwd=str(resolve_from_project(".")),
                check=True
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"❌ Hook {script.name} failed with exit code {e.returncode}")
            raise RuntimeError(f"Hook {script.name} failed.") from e

    def _run_in_chroot(self, script: Path):
        if not self.chroot:
            logger.warning(f"No chroot available to run chroot hook: {script.name}")
            return

        target_root = getattr(self.chroot, "target_root", None)
        if not target_root or not Path(target_root).exists():
            logger.warning(f"Target rootfs does not exist: {target_root}")
            return

        target_script = Path(target_root) / "tmp" / script.name
        try:
            target_script.parent.mkdir(parents=True, exist_ok=True)
            target_script.write_bytes(script.read_bytes())
            target_script.chmod(0o755)

            if getattr(self.chroot, "mode", "") == "mock":
                logger.info(f"    [Mock] Simulated chroot execution: /tmp/{script.name}")
                return

            if hasattr(self.chroot, "run_in_chroot"):
                self.chroot.run_in_chroot(["/bin/bash", f"/tmp/{script.name}"])
            elif hasattr(self.chroot, "run_command"):
                self.chroot.run_command(f"/tmp/{script.name}")
        except Exception as e:
            logger.error(f"❌ Hook {script.name} failed inside chroot: {e}")
            raise
        finally:
            if target_script.exists():
                try:
                    target_script.unlink()
                except Exception:
                    pass
