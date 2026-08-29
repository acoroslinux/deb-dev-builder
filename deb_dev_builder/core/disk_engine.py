import subprocess
import shutil
from pathlib import Path
from typing import Dict, Any, Optional
import logging

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

    def build_disk_image(self, target_format: str = "img") -> Path:
        out_path = self.workdir.parent.parent / "output" / f"{self.output_name}.img"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            out_path.touch()
            return out_path
            
        rootfs_size = self._calculate_image_size(self.target_root)
        efi_size = 300
        total_size = rootfs_size + efi_size + 4

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
                self.toolchain.run_in_build_host(["sload.f2fs", "-f", str(self.target_root), str(root_img)], check=False)
            else:
                self.toolchain.run_in_build_host(["mke2fs", "-t", "ext4", "-L", "ROOTFS", "-d", str(self.target_root), str(root_img)], check=True)
        else:
            subprocess.run(["truncate", "-s", f"{rootfs_size}M", str(root_img)], check=True)
            if fs_type == "btrfs":
                subprocess.run(["mkfs.btrfs", "-L", "ROOTFS", "-r", str(self.target_root), str(root_img)], check=True)
            elif fs_type == "f2fs":
                subprocess.run(["mkfs.f2fs", "-l", "ROOTFS", str(root_img)], check=True)
                subprocess.run(["sload.f2fs", "-f", str(self.target_root), str(root_img)], check=False)
            else:
                subprocess.run(["mke2fs", "-t", "ext4", "-L", "ROOTFS", "-d", str(self.target_root), str(root_img)], check=True)

        # Update rootfs_size because mkfs.btrfs -r dynamically expands the file size!
        rootfs_size = (root_img.stat().st_size // (1024 * 1024)) + 10
        efi_size = self.config.get("bootloader", {}).get("efi_size", 300)
        total_size = rootfs_size + efi_size + 4
        
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
        
        efi_fed_src = self.target_root / "boot" / "efi" / "EFI" / "debian"
        efi_boot_src = self.target_root / "boot" / "efi" / "EFI" / "BOOT"
        
        # Find kernel and initramfs inside rootfs /boot
        boot_dir = self.target_root / "boot"
        vmlinuz = next((f.name for f in boot_dir.glob("vmlinuz-*") if not f.name.endswith(".old") and "rescue" not in f.name), "vmlinuz")
        initrd = next((f.name for f in boot_dir.glob("initrd.img-*") if "rescue" not in f.name), "initrd.img")
        
        kernel_params = self.config.get("boot", {}).get("kernel_params", "quiet")
        kernel_params = " ".join([p for p in kernel_params.split() if p != "rd.live.image"])
        
        if bootloader_type == "systemd-boot":
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

        # Copy files to FAT image using mcopy
        if self.toolchain:
            self.toolchain.run_in_build_host(["mcopy", "-s", "-i", str(efi_img), f"{self.workdir}/efi_tmp/EFI", "::/"], check=True)
            if (self.workdir / "efi_tmp" / "loader").exists():
                self.toolchain.run_in_build_host(["mcopy", "-s", "-i", str(efi_img), f"{self.workdir}/efi_tmp/loader", "::/"], check=True)
            if (self.workdir / "efi_tmp" / vmlinuz).exists():
                self.toolchain.run_in_build_host(["mcopy", "-i", str(efi_img), f"{self.workdir}/efi_tmp/{vmlinuz}", "::/"], check=True)
                self.toolchain.run_in_build_host(["mcopy", "-i", str(efi_img), f"{self.workdir}/efi_tmp/{initrd}", "::/"], check=True)
        else:
            subprocess.run(["mcopy", "-s", "-i", str(efi_img), f"{self.workdir}/efi_tmp/EFI", "::/"], check=True)
            if (self.workdir / "efi_tmp" / "loader").exists():
                subprocess.run(["mcopy", "-s", "-i", str(efi_img), f"{self.workdir}/efi_tmp/loader", "::/"], check=True)
            if (self.workdir / "efi_tmp" / vmlinuz).exists():
                subprocess.run(["mcopy", "-i", str(efi_img), f"{self.workdir}/efi_tmp/{vmlinuz}", "::/"], check=True)
                subprocess.run(["mcopy", "-i", str(efi_img), f"{self.workdir}/efi_tmp/{initrd}", "::/"], check=True)

        logger.info(f"Building partitioned disk image ({total_size} MB)...")
        if self.toolchain:
            self.toolchain.run_in_build_host(["dd", "if=/dev/zero", f"of={out_path}", "bs=1M", f"count={total_size}", "status=none"], check=True)
            self.toolchain.run_in_build_host(["parted", "-s", str(out_path), "mktable", "gpt"], check=True)
            self.toolchain.run_in_build_host(["parted", "-s", str(out_path), "mkpart", "ESP", "fat32", "1MiB", f"{efi_size+1}MiB"], check=True)
            self.toolchain.run_in_build_host(["parted", "-s", str(out_path), "set", "1", "esp", "on"], check=True)
            self.toolchain.run_in_build_host(["parted", "-s", str(out_path), f"mkpart", "primary", fs_type, f"{efi_size+1}MiB", "100%"], check=True)
            # Inject partitions
            self.toolchain.run_in_build_host(["dd", f"if={efi_img}", f"of={out_path}", "bs=1M", "seek=1", "conv=notrunc", "status=none"], check=True)
            self.toolchain.run_in_build_host(["dd", f"if={root_img}", f"of={out_path}", "bs=1M", f"seek={efi_size+1}", "conv=notrunc", "status=none"], check=True)
        else:
            subprocess.run(["dd", "if=/dev/zero", f"of={out_path}", "bs=1M", f"count={total_size}", "status=none"], check=True)
            subprocess.run(["parted", "-s", str(out_path), "mktable", "gpt"], check=True)
            subprocess.run(["parted", "-s", str(out_path), "mkpart", "ESP", "fat32", "1MiB", f"{efi_size+1}MiB"], check=True)
            subprocess.run(["parted", "-s", str(out_path), "set", "1", "esp", "on"], check=True)
            subprocess.run(["parted", "-s", str(out_path), f"mkpart", "primary", fs_type, f"{efi_size+1}MiB", "100%"], check=True)
            subprocess.run(["dd", f"if={efi_img}", f"of={out_path}", "bs=1M", "seek=1", "conv=notrunc", "status=none"], check=True)
            subprocess.run(["dd", f"if={root_img}", f"of={out_path}", "bs=1M", f"seek={efi_size+1}", "conv=notrunc", "status=none"], check=True)

        bootloader_cfg = self.config.get("bootloader", "")
        bootloader_type = bootloader_cfg.get("type", "") if isinstance(bootloader_cfg, dict) else (bootloader_cfg or "")

        if bootloader_type.startswith("u-boot"):

            if bootloader_type == "u-boot-pinebookpro":

                logger.info("Injecting U-Boot for Pinebook Pro...")

                try:

                    if hasattr(self, "toolchain") and self.toolchain:

                        self.toolchain.run_in_build_host(["dd", f"if={self.target_root}/boot/u-boot/idbloader.img", f"of={out_path}", "bs=512", "seek=64", "conv=notrunc"], check=False)

                        self.toolchain.run_in_build_host(["dd", f"if={self.target_root}/boot/u-boot/u-boot.itb", f"of={out_path}", "bs=512", "seek=16384", "conv=notrunc"], check=False)

                    else:

                        import subprocess

                        subprocess.run(["dd", f"if={self.target_root}/boot/u-boot/idbloader.img", f"of={out_path}", "bs=512", "seek=64", "conv=notrunc"], check=False)

                        subprocess.run(["dd", f"if={self.target_root}/boot/u-boot/u-boot.itb", f"of={out_path}", "bs=512", "seek=16384", "conv=notrunc"], check=False)

                except Exception as e:

                    logger.warning(f"U-boot inject error (mock?): {e}")
        final_out = out_path
        if target_format != "img":
            vm_out = out_path.with_name(f"{self.output_name}.{target_format}")
            logger.info(f"Converting raw disk image to VM format: {target_format}...")
            if self.toolchain:
                self.toolchain.run_in_build_host(["qemu-img", "convert", "-f", "raw", "-O", target_format, str(out_path), str(vm_out)], check=True)
            else:
                subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", target_format, str(out_path), str(vm_out)], check=True)
            out_path.unlink()
            final_out = vm_out
            out_path = final_out

        compression = self.config.get("compression", "zstd")
        logger.info(f"Compressing disk image with {compression}...")
        
        final_path = out_path
        if compression == "xz":
            cmd = ["xz", "-z9", "-T0", str(out_path)]
            final_path = Path(f"{out_path}.xz")
        elif compression == "gz" or compression == "gzip":
            cmd = ["gzip", "-9", str(out_path)]
            final_path = Path(f"{out_path}.gz")
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
