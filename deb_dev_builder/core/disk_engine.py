import subprocess
import shutil
from pathlib import Path
from typing import Dict, Any, Optional
import logging
from deb_dev_builder.core.path_utils import resolve_output_path

logger = logging.getLogger("disk_engine")

class DiskEngineError(Exception):
    pass

class DiskEngine:
    def __init__(self, workdir: Path, target_root: Path, output_name: str, config: Dict[str, Any], mode: str, toolchain: Optional[Any] = None):
        self.workdir = Path(workdir).resolve()
        self.target_root = Path(target_root).resolve()
        self.output_name = output_name
        self.config = config
        self.mode = mode
        self.toolchain = toolchain

    def _calculate_image_size(self, rootfs: Path) -> int:
        if self.mode == "mock":
            return 1024
        out = subprocess.check_output(["du", "-sm", str(rootfs)])
        return int(out.split()[0]) + 600

    def _inject_boot_artifacts(self, image: Path) -> None:
        hardware = self.config.get("hardware", {})
        for entry in hardware.get("boot_artifacts", []):
            source = self.target_root / entry["source"].lstrip("/")
            if not source.is_file():
                if entry.get("required", True):
                    raise DiskEngineError(f"Required platform boot artifact is missing: {source}")
                logger.warning("Optional platform boot artifact is missing: %s", source)
                continue
            with source.open("rb") as input_file, image.open("r+b") as output_file:
                output_file.seek(entry["offset_kib"] * 1024)
                shutil.copyfileobj(input_file, output_file)

    def _validate_platform_files(self) -> None:
        hardware = self.config.get("hardware", {})
        missing = [
            self.target_root / relative.lstrip("/")
            for relative in hardware.get("required_files", [])
            if not (self.target_root / relative.lstrip("/")).is_file()
        ]
        missing.extend(
            self.target_root / entry["source"].lstrip("/")
            for entry in hardware.get("boot_artifacts", [])
            if entry.get("required", True)
            and not (self.target_root / entry["source"].lstrip("/")).is_file()
        )
        if missing:
            raise DiskEngineError(
                "Required platform files are missing: " + ", ".join(str(path) for path in missing)
            )

    def build_disk_image(self, target_format: str = "img") -> Path:
        requested_format = target_format.lower()
        raw_extension = ".img" if requested_format == "img" else ".raw"
        out_path = resolve_output_path(self.output_name, raw_extension)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            mock_path = resolve_output_path(self.output_name, ".img" if requested_format == "img" else f".{requested_format}")
            mock_path.parent.mkdir(parents=True, exist_ok=True)
            mock_path.touch()
            return mock_path

        self._validate_platform_files()

        efi_staging = self.workdir / "efi_tmp"
        if efi_staging.exists():
            shutil.rmtree(efi_staging)
            
        
        offline_repo_dir = self.config.get("offline_repo_dir")
        if offline_repo_dir and __import__('pathlib').Path(offline_repo_dir).exists():
            target_repo = self.target_root / "repo"
            target_repo.parent.mkdir(parents=True, exist_ok=True)
            if self.mode != "mock":
                __import__('shutil').copytree(offline_repo_dir, target_repo, dirs_exist_ok=True)

        rootfs_size = self._calculate_image_size(self.target_root)
        efi_size = 300
        partition_start = self.config.get("hardware", {}).get("partition_start_mib", 1)
        total_size = rootfs_size + efi_size + partition_start + 3

        efi_img = self.workdir / "efi.img"
        root_img = self.workdir / "root.img"
        
        logger.info(f"Generating {self.config.get('fs_type', 'ext4').upper()} root filesystem ({rootfs_size} MB)...")
        # Ensure target root has autorelabel
        (self.target_root / ".autorelabel").touch()
        
        fs_type = self.config.get("fs_type", "ext4")
        
        # Build root image directly from directory
        if self.toolchain:
            self.toolchain.run_in_build_host(["truncate", "-s", f"{rootfs_size}M", str(root_img)], check=True)
            if fs_type == "btrfs":
                self.toolchain.run_in_build_host(["mkfs.btrfs", "-L", "ROOTFS", "-r", str(self.target_root), str(root_img)], check=True)
            elif fs_type == "f2fs":
                self.toolchain.run_in_build_host(["mkfs.f2fs", "-l", "ROOTFS", str(root_img)], check=True)
                self.toolchain.run_in_build_host(["sload.f2fs", "-f", str(self.target_root), str(root_img)], check=True)
            elif fs_type == "xfs":
                self.toolchain.run_in_build_host(["mkfs.xfs", "-f", "-L", "ROOTFS", str(root_img)], check=True)
                self._populate_mounted_filesystem(root_img)
            else:
                self.toolchain.run_in_build_host(["mke2fs", "-t", "ext4", "-L", "ROOTFS", "-d", str(self.target_root), str(root_img)], check=True)
        else:
            subprocess.run(["truncate", "-s", f"{rootfs_size}M", str(root_img)], check=True)
            if fs_type == "btrfs":
                subprocess.run(["mkfs.btrfs", "-L", "ROOTFS", "-r", str(self.target_root), str(root_img)], check=True)
            elif fs_type == "f2fs":
                subprocess.run(["mkfs.f2fs", "-l", "ROOTFS", str(root_img)], check=True)
                subprocess.run(["sload.f2fs", "-f", str(self.target_root), str(root_img)], check=True)
            elif fs_type == "xfs":
                subprocess.run(["mkfs.xfs", "-f", "-L", "ROOTFS", str(root_img)], check=True)
                self._populate_mounted_filesystem(root_img)
            else:
                subprocess.run(["mke2fs", "-t", "ext4", "-L", "ROOTFS", "-d", str(self.target_root), str(root_img)], check=True)

        # Update rootfs_size because mkfs.btrfs -r dynamically expands the file size!
        rootfs_size = (root_img.stat().st_size // (1024 * 1024)) + 10
        efi_size = self.config.get("bootloader", {}).get("efi_size", 300)
        total_size = rootfs_size + efi_size + partition_start + 3
        
        logger.info(f"Generating FAT32 EFI filesystem ({efi_size} MB)...")
        # Create FAT image
        if self.toolchain:
            self.toolchain.run_in_build_host(["truncate", "-s", f"{efi_size}M", str(efi_img)], check=True)
            self.toolchain.run_in_build_host(["mkfs.fat", "-F", "32", str(efi_img)], check=True)
        else:
            subprocess.run(["truncate", "-s", f"{efi_size}M", str(efi_img)], check=True)
            subprocess.run(["mkfs.fat", "-F", "32", str(efi_img)], check=True)

        # Copy EFI bootloader into FAT image using mtools
        # First, ensure we have the EFI files
        efi_boot_dir = self.workdir / "efi_tmp" / "EFI" / "BOOT"
        efi_boot_dir.mkdir(parents=True, exist_ok=True)
        
        bootloader_type = self.config.get("bootloader", {}).get("type", "grub2-hybrid")
        platform_boot = bootloader_type.startswith("u-boot") or bootloader_type == "asahi"
        
        efi_fed_src = self.target_root / "boot" / "efi" / "EFI" / "debian"
        efi_boot_src = self.target_root / "boot" / "efi" / "EFI" / "BOOT"
        
        # Find kernel and initramfs inside rootfs /boot
        boot_dir = self.target_root / "boot"
        vmlinuz = next((f.name for f in boot_dir.glob("vmlinuz-*") if not f.name.endswith(".old") and "rescue" not in f.name), "vmlinuz")
        initrd = next((f.name for f in boot_dir.glob("initrd.img-*") if "rescue" not in f.name), "initrd.img")
        
        kernel_params = self.config.get("boot", {}).get("kernel_params", "quiet")
        kernel_params = " ".join([p for p in kernel_params.split() if p != "rd.live.image"])
        
        if platform_boot:
            firmware_tree = self.config.get("hardware", {}).get("firmware_tree")
            if firmware_tree:
                firmware_source = self.target_root / firmware_tree.lstrip("/")
                if not firmware_source.is_dir():
                    raise DiskEngineError(f"Required platform firmware tree is missing: {firmware_source}")
                shutil.copytree(firmware_source, self.workdir / "efi_tmp", dirs_exist_ok=True)
            boot_staging = self.workdir / "efi_tmp" / "boot"
            boot_staging.mkdir(parents=True, exist_ok=True)
            for source_name in (vmlinuz, initrd):
                source = boot_dir / source_name
                if not source.is_file():
                    raise DiskEngineError(f"Required ARM boot file is missing: {source}")
                shutil.copy2(source, boot_staging / source_name)
            dtbs = boot_dir / "dtbs"
            if dtbs.is_dir():
                shutil.copytree(dtbs, boot_staging / "dtbs", dirs_exist_ok=True)
            extlinux = self.workdir / "efi_tmp" / "extlinux"
            extlinux.mkdir(parents=True, exist_ok=True)
            (extlinux / "extlinux.conf").write_text(
                "DEFAULT debian\nTIMEOUT 30\n\nLABEL debian\n"
                "  MENU LABEL Debian GNU/Linux\n"
                f"  LINUX /boot/{vmlinuz}\n"
                f"  INITRD /boot/{initrd}\n"
                f"  APPEND root=LABEL=ROOTFS rw {kernel_params}\n"
            )
        elif bootloader_type == "systemd-boot":
            import shutil
            # Install systemd-boot
            sd_boot_src = self.target_root / "usr" / "lib" / "systemd" / "boot" / "efi" / "systemd-bootx64.efi"
            if sd_boot_src.exists():
                shutil.copy2(sd_boot_src, efi_boot_dir / "BOOTX64.EFI")
            
            # Copy kernel and initrd to ESP (systemd-boot requires them on the same FAT partition)
            shutil.copy2(boot_dir / vmlinuz, self.workdir / "efi_tmp" / vmlinuz)
            shutil.copy2(boot_dir / initrd, self.workdir / "efi_tmp" / initrd)
            
            # Create loader/loader.conf
            loader_dir = self.workdir / "efi_tmp" / "loader"
            loader_dir.mkdir(parents=True, exist_ok=True)
            (loader_dir / "loader.conf").write_text("default debian\\ntimeout 3\\n")
            
            # Create loader/entries/debian.conf
            entries_dir = loader_dir / "entries"
            entries_dir.mkdir(parents=True, exist_ok=True)
            (entries_dir / "debian.conf").write_text(f"""title Debian Linux
linux /{vmlinuz}
initrd /{initrd}
options root=LABEL=ROOTFS rw {kernel_params}
""")
        else:
            if efi_fed_src.exists():
                shutil.copytree(efi_fed_src, self.workdir / "efi_tmp" / "EFI" / "debian", dirs_exist_ok=True)
            if efi_boot_src.exists():
                shutil.copytree(efi_boot_src, efi_boot_dir, dirs_exist_ok=True)
                
            # Ensure BOOTX64.EFI exists
            bootx64 = efi_boot_dir / "BOOTX64.EFI"
            if not bootx64.exists():
                shim = self.workdir / "efi_tmp" / "EFI" / "debian" / "shimx64.efi"
                grub = self.workdir / "efi_tmp" / "EFI" / "debian" / "grubx64.efi"
                if shim.exists():
                    shutil.copy2(shim, bootx64)
                elif grub.exists():
                    shutil.copy2(grub, bootx64)
                if grub.exists():
                    shutil.copy2(grub, efi_boot_dir / "grubx64.efi")

            # Create basic grub.cfg for disk image boot
            grub_cfg = self.workdir / "efi_tmp" / "EFI" / "debian" / "grub.cfg"
            grub_cfg.parent.mkdir(parents=True, exist_ok=True)
            
            grub_cfg.write_text(f"""
search --no-floppy --set=root --label ROOTFS
set prefix=($root)/boot/grub2

menuentry "Debian Linux" {{
    linux /boot/{vmlinuz} root=LABEL=ROOTFS rw {kernel_params}
    initrd /boot/{initrd}
}}
""")

        if not platform_boot and not any(
            path.is_file() and path.stat().st_size > 0 for path in efi_boot_dir.glob("*.EFI")
        ):
            raise DiskEngineError(f"No removable UEFI bootloader was installed in {efi_boot_dir}")

        # Copy files to FAT image using mcopy
        for staged_item in sorted((self.workdir / "efi_tmp").iterdir()):
            command = ["mcopy", "-s", "-i", str(efi_img), str(staged_item), "::/"]
            if self.toolchain:
                self.toolchain.run_in_build_host(command, check=True)
            else:
                subprocess.run(command, check=True)

        logger.info(f"Building partitioned disk image ({total_size} MB)...")
        if self.toolchain:
            self.toolchain.run_in_build_host(["dd", "if=/dev/zero", f"of={out_path}", "bs=1M", f"count={total_size}", "status=none"], check=True)
            self.toolchain.run_in_build_host(["parted", "-s", str(out_path), "mktable", "gpt"], check=True)
            self.toolchain.run_in_build_host(["parted", "-s", str(out_path), "mkpart", "ESP", "fat32", f"{partition_start}MiB", f"{efi_size+partition_start}MiB"], check=True)
            self.toolchain.run_in_build_host(["parted", "-s", str(out_path), "set", "1", "esp", "on"], check=True)
            self.toolchain.run_in_build_host(["parted", "-s", str(out_path), f"mkpart", "primary", fs_type, f"{efi_size+partition_start}MiB", "100%"], check=True)
            # Inject partitions
            self.toolchain.run_in_build_host(["dd", f"if={efi_img}", f"of={out_path}", "bs=1M", f"seek={partition_start}", "conv=notrunc", "status=none"], check=True)
            self.toolchain.run_in_build_host(["dd", f"if={root_img}", f"of={out_path}", "bs=1M", f"seek={efi_size+partition_start}", "conv=notrunc", "status=none"], check=True)
        else:
            subprocess.run(["dd", "if=/dev/zero", f"of={out_path}", "bs=1M", f"count={total_size}", "status=none"], check=True)
            subprocess.run(["parted", "-s", str(out_path), "mktable", "gpt"], check=True)
            subprocess.run(["parted", "-s", str(out_path), "mkpart", "ESP", "fat32", f"{partition_start}MiB", f"{efi_size+partition_start}MiB"], check=True)
            subprocess.run(["parted", "-s", str(out_path), "set", "1", "esp", "on"], check=True)
            subprocess.run(["parted", "-s", str(out_path), f"mkpart", "primary", fs_type, f"{efi_size+partition_start}MiB", "100%"], check=True)
            subprocess.run(["dd", f"if={efi_img}", f"of={out_path}", "bs=1M", f"seek={partition_start}", "conv=notrunc", "status=none"], check=True)
            subprocess.run(["dd", f"if={root_img}", f"of={out_path}", "bs=1M", f"seek={efi_size+partition_start}", "conv=notrunc", "status=none"], check=True)

        self._inject_boot_artifacts(out_path)
        final_out = out_path
        if requested_format not in {"img", "raw"}:
            vm_out = resolve_output_path(self.output_name, f".{requested_format}")
            logger.info(f"Converting raw disk image to VM format: {target_format}...")
            qemu_format = {"vhd": "vpc"}.get(requested_format, requested_format)
            if self.toolchain:
                self.toolchain.run_in_build_host(["qemu-img", "convert", "-f", "raw", "-O", qemu_format, str(out_path), str(vm_out)], check=True)
            else:
                subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", qemu_format, str(out_path), str(vm_out)], check=True)
            out_path.unlink()
            final_out = vm_out
            out_path = final_out

        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise DiskEngineError(f"Disk image was not created: {out_path}")
        if requested_format not in {"img", "raw"}:
            validator = ["qemu-img", "info", "--output=json", str(out_path)]
            if self.toolchain:
                self.toolchain.run_in_build_host(validator, check=True)
            else:
                subprocess.run(validator, check=True, capture_output=True)

        compression = self.config.get("compression")
        if not compression:
            return out_path
        logger.info(f"Compressing disk image with {compression}...")
        
        final_path = out_path
        if compression == "xz":
            cmd = ["xz", "-z9", "-T0", "-f", str(out_path)]
            final_path = Path(f"{out_path}.xz")
        elif compression == "gz" or compression == "gzip":
            cmd = ["gzip", "-9", "-f", str(out_path)]
            final_path = Path(f"{out_path}.gz")
        elif compression == "lz4":
            final_path = Path(f"{out_path}.lz4")
            cmd = ["lz4", "-9", "-f", "--rm", str(out_path), str(final_path)]
        else: # zstd
            zstd_level = "-3" if self.config.get("fast_mode", False) else "-19"
            cmd = ["zstd", zstd_level, "-f", "-T0", "-q", "--rm", str(out_path)]
            final_path = Path(f"{out_path}.zst")
            
        if self.toolchain:
            self.toolchain.run_in_build_host(cmd, check=True)
        else:
            subprocess.run(cmd, check=True)
            
        logger.info(f"Disk image generated successfully at {final_path}")
        return final_path

    def _populate_mounted_filesystem(self, image: Path) -> None:
        """Populate filesystems whose mkfs utility cannot import a directory."""
        mountpoint = self.workdir / "rootfs_mount"
        mountpoint.mkdir(parents=True, exist_ok=True)
        runner = self.toolchain.run_in_build_host if self.toolchain else lambda cmd, check=True: subprocess.run(cmd, check=check)
        runner(["mount", "-o", "loop", str(image), str(mountpoint)], check=True)
        try:
            runner(["cp", "-a", f"{self.target_root}/.", str(mountpoint)], check=True)
        finally:
            runner(["umount", str(mountpoint)], check=True)
