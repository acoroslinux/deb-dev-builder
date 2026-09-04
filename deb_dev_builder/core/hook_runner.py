import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from deb_dev_builder.core.path_utils import resolve_from_project


class HookError(RuntimeError):
    """Raised when a build hook fails or has an invalid definition."""


class HookRunner:
    """Run ordered, executable hooks from ``<root>/<phase>.d`` directories."""

    PHASES = (
        "preflight",
        "post-toolchain",
        "pre-bootstrap",
        "post-bootstrap",
        "pre-chroot-mount",
        "post-chroot-mount",
        "pre-apt",
        "post-apt",
        "pre-packages",
        "post-packages",
        "pre-customize",
        "post-customize",
        "pre-installer",
        "post-installer",
        "pre-unmount",
        "post-unmount",
        "pre-cleanup",
        "post-cleanup",
        "pre-artifact",
        "post-artifact",
        "on-error",
        "cleanup",
    )

    def __init__(
        self,
        hooks_dir: Optional[str],
        config: Dict[str, Any],
        workdir: Path,
        target_root: Path,
        mode: str,
        enabled: bool = True,
    ):
        configured = hooks_dir or config.get("hooks", {}).get("directory", "hooks")
        candidate = Path(configured)
        self.root = candidate if candidate.is_absolute() else resolve_from_project(candidate)
        chroot_configured = config.get("hooks", {}).get("chroot_directory", "chroot")
        chroot_candidate = Path(chroot_configured)
        self.chroot_root = (self.root / chroot_candidate) if not chroot_candidate.is_absolute() else chroot_candidate
        self.config = config
        self.workdir = Path(workdir).resolve()
        self.target_root = Path(target_root).resolve()
        self.mode = mode.lower()
        self.enabled = enabled and config.get("hooks", {}).get("enabled", True)

    def scripts_for(self, phase: str):
        if phase not in self.PHASES:
            raise HookError(f"Unknown hook phase: {phase}")
        phase_dir = self.root / f"{phase}.d"
        if not self.enabled or not phase_dir.is_dir():
            return []
        return sorted(
            path for path in phase_dir.iterdir()
            if path.is_file() and os.access(path, os.X_OK) and not path.name.startswith(".")
        )

    def _environment(self, phase: str, artifact: Optional[Path], error: Optional[BaseException], scope: str = "host"):
        environment = os.environ.copy()
        environment.update({
            "DEB_DEV_HOOK_PHASE": phase,
            "DEB_DEV_HOOK_SCOPE": scope,
            "DEB_DEV_WORKDIR": str(self.workdir),
            "DEB_DEV_TARGET_ROOT": str(self.target_root),
            "DEB_DEV_ARCH": str(self.config.get("architecture", "")),
            "DEB_DEV_DPKG_ARCH": str(self.config.get("dpkg_arch", "")),
            "DEB_DEV_DISTRO": str(self.config.get("distro", "")),
            "DEB_DEV_SUITE": str(self.config.get("suite", "")),
            "DEB_DEV_OUTPUT_FORMAT": str(self.config.get("output_format", "")),
            "DEB_DEV_ARTIFACT": str(artifact or ""),
            "DEB_DEV_ERROR": str(error or ""),
        })
        return environment

    def run(self, phase: str, artifact: Optional[Path] = None, error: Optional[BaseException] = None) -> None:
        scripts = self.scripts_for(phase)
        if self.mode == "mock":
            return
        environment = self._environment(phase, artifact, error)
        for script in scripts:
            try:
                subprocess.run(
                    [str(script)],
                    cwd=str(self.workdir),
                    env=environment,
                    check=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise HookError(f"Hook failed in phase {phase}: {script}: {exc}") from exc

    def chroot_scripts_for(self, phase: str):
        if phase not in self.PHASES:
            raise HookError(f"Unknown hook phase: {phase}")
        phase_dir = self.chroot_root / f"{phase}.d"
        if not self.enabled or not phase_dir.is_dir():
            return []
        return sorted(
            path for path in phase_dir.iterdir()
            if path.is_file() and os.access(path, os.X_OK) and not path.name.startswith(".")
        )

    def run_chroot(self, phase: str, chroot, artifact: Optional[Path] = None, error: Optional[BaseException] = None) -> None:
        """Copy and execute executable hooks inside the target root."""
        scripts = self.chroot_scripts_for(phase)
        if self.mode == "mock" or not scripts:
            return
        hook_dir = self.target_root / "run" / "deb-dev-builder-hooks" / phase
        hook_dir.mkdir(parents=True, exist_ok=True)
        environment = self._environment(phase, artifact, error, scope="chroot")
        try:
            for script in scripts:
                staged = hook_dir / script.name
                shutil.copy2(script, staged)
                staged.chmod(0o755)
                chroot.run_in_chroot([f"/run/deb-dev-builder-hooks/{phase}/{script.name}"], check=True, env=environment)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise HookError(f"Chroot hook failed in phase {phase}: {script}: {exc}") from exc
        finally:
            shutil.rmtree(self.target_root / "run" / "deb-dev-builder-hooks", ignore_errors=True)
