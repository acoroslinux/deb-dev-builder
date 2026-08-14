import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any
import logging
from deb_dev_builder.core.path_utils import resolve_from_project

logger = logging.getLogger("disk_engine")

class DiskEngineError(Exception):
    """Raised when a disk image cannot be created."""
    pass

class DiskEngine:
    def __init__(self, workdir: Path, target_root: Path, output_name: str, config: Dict[str, Any], mode: str):
        self.workdir = Path(workdir)
        self.target_root = Path(target_root)
        self.output_name = output_name
        self.config = config
        self.mode = mode.lower()

    def build_disk_image(self, target_format: str = "img") -> Path:
        fmt_clean = target_format.lower().lstrip(".")
        if fmt_clean in {"img", "raw"}:
            out_ext = "img"
        else:
            out_ext = fmt_clean

        out_path = resolve_from_project(f"output/{self.output_name}.{out_ext}")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            out_path.touch()
            return out_path

        if not self.target_root.is_dir():
            raise DiskEngineError(f"Root filesystem directory does not exist: {self.target_root}")

        if shutil.which("mkfs.ext4") is None:
            raise DiskEngineError("mkfs.ext4 is required to build a disk image.")

        raw_path = self.workdir / f"{self.output_name}.raw"
        size = self.config.get("disk_image_size", "4G")
        hostname = self.config.get("hostname", "deb-dev-rootfs")

        logger.info(f"Creating raw disk image ({size}) at: {raw_path}")
        subprocess.run(["truncate", "-s", str(size), str(raw_path)], check=True)

        built_partitioned = False
        # Attempt GPT partitioning + loop mount if running as root
        if os.geteuid() == 0 and shutil.which("sfdisk") and shutil.which("losetup"):
            try:
                self._build_partitioned_disk(raw_path, hostname)
                built_partitioned = True
            except Exception as e:
                logger.warning(f"Partitioned disk image creation failed ({e}); falling back to single-partition ext4 image.")

        if not built_partitioned:
            subprocess.run(
                ["mkfs.ext4", "-F", "-L", hostname, "-d", str(self.target_root), str(raw_path)],
                check=True,
            )

        if out_ext in {"img", "raw"}:
            shutil.move(str(raw_path), str(out_path))
            return out_path

        # Convert raw disk to requested virtual machine image format (qcow2, vmdk, vhd, vdi)
        return self._convert_disk_format(raw_path, out_path, out_ext)

    def _convert_disk_format(self, raw_path: Path, out_path: Path, target_fmt: str) -> Path:
        qemu_img = shutil.which("qemu-img")
        if not qemu_img:
            raise DiskEngineError(f"qemu-img is required to convert raw disk to format '{target_fmt}'.")

        fmt_map = {
            "qcow2": "qcow2",
            "vmdk": "vmdk",
            "vhd": "vpc",
            "vhdx": "vhdx",
            "vdi": "vdi",
        }
        qemu_target_fmt = fmt_map.get(target_fmt, target_fmt)

        logger.info(f"Converting raw disk image to {target_fmt.upper()} format using qemu-img...")
        cmd = [qemu_img, "convert", "-f", "raw", "-O", qemu_target_fmt, str(raw_path), str(out_path)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        raw_path.unlink(missing_ok=True)

        if res.returncode != 0:
            raise DiskEngineError(f"qemu-img conversion failed: {res.stderr}")

        return out_path

    def _build_partitioned_disk(self, out_path: Path, label: str) -> None:
        """Create a GPT partitioned image with EFI System Partition (ESP) and ext4 root partition."""
        sfdisk_script = (
            "label: gpt\n"
            "type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, size=256M, name=\"EFI System Partition\"\n"
            "type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name=\"Linux rootfs\"\n"
        )
        proc = subprocess.run(["sfdisk", str(out_path)], input=sfdisk_script, text=True, capture_output=True)
        if proc.returncode != 0:
            raise DiskEngineError(f"sfdisk partitioning failed: {proc.stderr}")

        loop_res = subprocess.run(["losetup", "--show", "-f", "-P", str(out_path)], capture_output=True, text=True, check=True)
        loop_dev = loop_res.stdout.strip()

        try:
            p1 = f"{loop_dev}p1"
            p2 = f"{loop_dev}p2"

            if shutil.which("mkfs.vfat"):
                subprocess.run(["mkfs.vfat", "-F", "32", "-n", "EFI", p1], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["mkfs.ext4", "-F", "-L", label, "-d", str(self.target_root), p2], check=True, stdout=subprocess.DEVNULL)
        finally:
            subprocess.run(["losetup", "-d", loop_dev], check=False)
