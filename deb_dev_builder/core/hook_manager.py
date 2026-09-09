import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from deb_dev_builder.core.path_utils import resolve_from_project

logger = logging.getLogger("hook_manager")


class HookError(RuntimeError):
    """A hook phase could not be executed successfully."""


class HookManager:
    """Execute the three existing phases without modifying the hook sources."""

    STAGES = ("pre-chroot", "chroot", "post-chroot")

    def __init__(
        self, chroot_manager, config: Optional[Dict[str, Any]] = None,
        *, hooks_dir: Optional[str] = None, workdir: Optional[Path] = None,
        enabled: bool = True,
    ):
        self.chroot = chroot_manager
        self.config = config or {}
        settings = self.config.get("hooks", {})
        self.hooks_base = resolve_from_project(hooks_dir or settings.get("directory", "configs/hooks"))
        self.target_root = Path(self.chroot.target_root).resolve()
        self.workdir = Path(workdir or self.target_root.parent).resolve()
        self.enabled = enabled and settings.get("enabled", True)
        self.mode = self.chroot.mode.lower()

    def scripts_for(self, stage: str):
        if stage not in self.STAGES:
            raise HookError(f"Unknown hook phase: {stage}")
        directory = self.hooks_base / stage
        if not self.enabled or not directory.is_dir():
            return []
        return sorted(
            script for script in directory.iterdir()
            if script.is_file() and not script.is_symlink()
            and script.suffix == ".sh" and not script.name.startswith(".")
            and os.access(script, os.X_OK)
        )

    def _environment(self, stage: str, artifact: Optional[Path]):
        environment = os.environ.copy()
        arch = str(self.config.get("architecture") or self.config.get("arch", ""))
        environment.update({
            "TARGET_ROOT": str(self.target_root),
            "CHROOT_PATH": str(self.target_root),
            "BUILD_ARCH": arch,
            "BUILD_DESKTOP": str(self.config.get("desktop") or ""),
            "HOOK_PHASE": stage,
            "DEB_DEV_HOOK_PHASE": stage,
            "DEB_DEV_HOOK_SCOPE": "chroot" if stage == "chroot" else "host",
            "DEB_DEV_WORKDIR": str(self.workdir),
            "DEB_DEV_TARGET_ROOT": str(self.target_root),
            "DEB_DEV_ARCH": arch,
            "DEB_DEV_DPKG_ARCH": str(self.config.get("dpkg_arch", "")),
            "DEB_DEV_DISTRO": str(self.config.get("distro", "")),
            "DEB_DEV_SUITE": str(self.config.get("suite", "")),
            "DEB_DEV_OUTPUT_FORMAT": str(self.config.get("output_format", "")),
            "DEB_DEV_ARTIFACT": str(Path(artifact).resolve()) if artifact else "",
        })
        return environment

    def run_stage(self, stage: str, *, artifact: Optional[Path] = None):
        scripts = self.scripts_for(stage)
        if self.mode == "mock" or not scripts:
            return
        environment = self._environment(stage, artifact)
        for script in scripts:
            logger.info("Running %s hook: %s", stage, script.name)
            try:
                if stage == "chroot":
                    self._run_in_chroot(script, environment)
                else:
                    subprocess.run(
                        ["/bin/bash", str(script)], check=True,
                        cwd=str(resolve_from_project(".")), env=environment,
                    )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise HookError(f"Hook failed in phase {stage}: {script}: {exc}") from exc

    def _run_in_chroot(self, script: Path, environment: dict):
        if not self.target_root.is_dir() or not self.chroot.is_mounted:
            raise HookError("Chroot hooks require an existing, mounted target rootfs")
        temporary = self.target_root / "tmp"
        # Never stage through a rootfs link that resolves into the host.
        if temporary.is_symlink():
            raise HookError("Cannot stage hooks through a symlink at target /tmp")
        temporary.mkdir(parents=True, exist_ok=True)
        # A unique, non-hidden .sh file survives the bundled /tmp cleanup hook
        # and never overwrites a pre-existing file with the source script name.
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix="deb-dev-builder-hook-", suffix=".sh",
            dir=temporary, delete=False,
        ) as handle:
            staged = Path(handle.name)
        try:
            staged.write_bytes(script.read_bytes())
            staged.chmod(0o700)
            self.chroot.run_in_chroot(
                ["/bin/bash", f"/tmp/{staged.name}"], check=True, env=environment,
            )
        finally:
            staged.unlink(missing_ok=True)
