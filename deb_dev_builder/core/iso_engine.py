import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Tuple, Any
import logging
from deb_dev_builder.core.toolchain_manager import ToolchainManager
from deb_dev_builder.core.path_utils import resolve_from_project

logger = logging.getLogger("iso_engine")

_ARCH_EFI_MAP = {
    "amd64":   ("x86_64-efi",  "BOOTX64.EFI"),
    "x86_64":  ("x86_64-efi",  "BOOTX64.EFI"),
    "i386":    ("x86_64-efi",  "BOOTX64.EFI"),
    "i686":    ("x86_64-efi",  "BOOTX64.EFI"),
    "arm64":   ("arm64-efi",   "BOOTAA64.EFI"),
    "aarch64": ("arm64-efi",   "BOOTAA64.EFI"),
    "riscv64": ("riscv64-efi", "BOOTRISCV64.EFI"),
}

_BIOS_ARCHES = {"amd64", "x86_64", "i386", "i686", "x86"}

class ISOEngineError(Exception):
    pass

class ISOEngine:
    def __init__(self, workdir: Path, target_root: Path, output_name: str, config: Dict[str, Any], mode: str, toolchain: ToolchainManager):
        self.workdir = Path(workdir)
        self.target_root = Path(target_root)
        self.output_name = output_name
        self.config = config
        self.mode = mode
        self.toolchain = toolchain
        self.iso_staging = self.workdir / "iso_root"
        self.arch = config.get("architecture", "amd64")

    def _get_iso_label(self) -> str:
        return self.config.get("iso_label", self.config.get("system", {}).get("iso_label", "DEB-DEV-LIVE"))

    def _get_kernel_params(self) -> str:
        base = self.config.get("kernel_params", self.config.get("boot", {}).get("kernel_params", "boot=live components quiet splash"))
        if "boot=live" not in base:
            base = f"boot=live {base}"
        return base

    def _find_kernel_and_initramfs(self) -> Tuple[str, str]:
        boot_dir = self.target_root / "boot"
        kernel = None
        initramfs = None

        if boot_dir.exists():
            for f in sorted(boot_dir.iterdir()):
                if f.is_file():
                    if f.name.startswith("vmlinuz"):
                        kernel = f.name
                    elif f.name.startswith("initrd.img") or f.name.startswith("initrd"):
                        initramfs = f.name

        return kernel or "vmlinuz", initramfs or "initrd.img"

    def _create_squashfs(self, source_dir: Path, output_path: Path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            output_path.touch()
            return

        if output_path.exists():
            output_path.unlink()

        compression = self.config.get("compression", "zstd")
        num_cpus = os.cpu_count() or 4
        logger.info(f"Creating SquashFS with {compression} compression using {num_cpus} cores...")
        self.toolchain.run_tool(
            "mksquashfs",
            [
                str(source_dir),
                str(output_path),
                "-comp", compression,
                "-b", "1M",
                "-processors", str(num_cpus),
                "-noappend",
                "-e", "proc", "sys", "dev", "tmp", "var/cache/apt"
            ],
        )

    def generate_grub_efi_image(self):
        efiboot_img = self.iso_staging / "boot" / "grub" / "efiboot.img"
        efiboot_img.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            efiboot_img.touch()
            return

        formats_to_build = []
        if self.arch in ["i686", "i386", "x86", "x86_64", "amd64"]:
            formats_to_build = [
                ("x86_64-efi", "BOOTX64.EFI"),
                ("i386-efi",   "BOOTIA32.EFI"),
            ]
        else:
            primary_fmt, primary_file = _ARCH_EFI_MAP.get(self.arch, ("x86_64-efi", "BOOTX64.EFI"))
            formats_to_build = [(primary_fmt, primary_file)]

        efi_tmp = self.workdir / "tmp_efi"
        efi_tmp.mkdir(parents=True, exist_ok=True)

        created_binaries = []
        for fmt, boot_filename in formats_to_build:
            out_binary = efi_tmp / boot_filename
            built = False
            if shutil.which("grub-mkstandalone"):
                grub_cfg = self.iso_staging / "boot" / "grub" / "grub.cfg"
                res = subprocess.run([
                    "grub-mkstandalone", f"--format={fmt}",
                    f"-o={out_binary}", f"boot/grub/grub.cfg={grub_cfg}"
                ], capture_output=True)
                if res.returncode == 0 and out_binary.exists():
                    built = True

            if built or out_binary.exists():
                created_binaries.append((out_binary, boot_filename))

        if created_binaries:
            iso_efi_dir = self.iso_staging / "EFI" / "BOOT"
            iso_efi_dir.mkdir(parents=True, exist_ok=True)
            for binary_path, filename in created_binaries:
                shutil.copy2(binary_path, iso_efi_dir / filename)

            subprocess.run(["truncate", "-s", "32M", str(efiboot_img)], check=True)
            if shutil.which("mformat") and shutil.which("mcopy"):
                subprocess.run(["mformat", "-i", str(efiboot_img), "-h", "32", "-t", "32", "-n", "64", "-c", "1", "::"], check=True, capture_output=True)
                subprocess.run(["mmd", "-i", str(efiboot_img), "::/EFI"], capture_output=True)
                subprocess.run(["mmd", "-i", str(efiboot_img), "::/EFI/BOOT"], capture_output=True)
                for binary_path, filename in created_binaries:
                    subprocess.run(["mcopy", "-i", str(efiboot_img), str(binary_path), f"::/EFI/BOOT/{filename}"], check=True, capture_output=True)

        shutil.rmtree(efi_tmp, ignore_errors=True)

    def build_iso(self) -> Path:
        self.iso_staging.mkdir(parents=True, exist_ok=True)
        (self.iso_staging / "live").mkdir(parents=True, exist_ok=True)
        (self.iso_staging / "isolinux").mkdir(parents=True, exist_ok=True)
        (self.iso_staging / "boot" / "grub").mkdir(parents=True, exist_ok=True)

        kernel, initramfs = self._find_kernel_and_initramfs()
        if self.mode != "mock":
            src_kernel = self.target_root / "boot" / kernel
            src_initramfs = self.target_root / "boot" / initramfs
            if src_kernel.exists():
                shutil.copy2(src_kernel, self.iso_staging / "live" / "vmlinuz")
            if src_initramfs.exists():
                shutil.copy2(src_initramfs, self.iso_staging / "live" / "initrd.img")

        squashfs_path = self.iso_staging / "live" / "filesystem.squashfs"
        self._create_squashfs(self.target_root, squashfs_path)

        iso_label = self._get_iso_label()
        kernel_params = self._get_kernel_params()

        with open(self.iso_staging / "boot" / "grub" / "grub.cfg", "w") as f:
            f.write(
                f"set default=0\nset timeout=5\n\n"
                f"menuentry 'Start Live System' {{\n"
                f"    linux /live/vmlinuz {kernel_params}\n"
                f"    initrd /live/initrd.img\n"
                f"}}\n"
            )

        self.generate_grub_efi_image()

        iso_path = resolve_from_project(f"output/{self.output_name}.iso")
        iso_path.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            iso_path.touch()
        else:
            self.toolchain.run_tool(
                "xorriso",
                [
                    "-as", "mkisofs",
                    "-V", iso_label,
                    "-rock", "-joliet",
                    "-eltorito-boot", "isolinux/isolinux.bin",
                    "-eltorito-catalog", "isolinux/boot.cat",
                    "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table",
                    "-eltorito-alt-boot",
                    "-e", "boot/grub/efiboot.img",
                    "-no-emul-boot", "-isohybrid-gpt-basdat",
                    "-o", str(iso_path),
                    str(self.iso_staging),
                ],
                check=False
            )

        return iso_path

    def build_tarball(self) -> Path:
        tar_path = resolve_from_project(f"output/stage3_seeds/{self.output_name}.tar.xz")
        tar_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            tar_path.touch()
        else:
            subprocess.run(["tar", "cJpf", str(tar_path), "-C", str(self.target_root), "."], check=True)
        return tar_path
