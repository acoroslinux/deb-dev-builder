import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any
import logging
from deb_dev_builder.core.path_utils import resolve_from_project

logger = logging.getLogger("container_engine")

class ContainerEngineError(Exception):
    """Raised when an OCI container archive build fails."""
    pass

class ContainerEngine:
    """
    Builds OCI-compliant container layer archives (.oci.tar) from a bootstrapped Debian/Devuan rootfs.
    """
    def __init__(self, target_root: Path, output_name: str, config: Dict[str, Any], mode: str = "real"):
        self.target_root = Path(target_root)
        self.output_name = output_name
        self.config = config
        self.mode = mode.lower()

    def build_oci_archive(self) -> Path:
        out_path = resolve_from_project(f"output/{self.output_name}.oci.tar")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            out_path.touch()
            return out_path

        if not self.target_root.is_dir():
            raise ContainerEngineError(f"Target rootfs does not exist: {self.target_root}")

        logger.info(f"📦 Packaging OCI container archive to: {out_path}")
        cmd = [
            "tar", "cf", str(out_path),
            "--exclude=./proc/*", "--exclude=./sys/*", "--exclude=./dev/*",
            "--exclude=./run/*", "--exclude=./tmp/*", "--exclude=./var/cache/apt/*",
            "-C", str(self.target_root), "."
        ]

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise ContainerEngineError(f"Failed to create OCI archive tarball: {res.stderr}")

        return out_path
