import os
import re
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
        self.mode = mode.lower()
        self.toolchain = toolchain
        self.iso_staging = self.workdir / "iso_root"
        self.arch = config.get("architecture", "amd64")

    def get_bootloader_type(self) -> str:
        bootloader = self.config.get("bootloader", {})
        if isinstance(bootloader, str):
            raw_type = bootloader
        else:
            raw_type = bootloader.get("type") if isinstance(bootloader, dict) else None
        if not raw_type:
            raw_type = self.config.get("bootloader_type") or self.config.get("boot", {}).get("type") or "grub2-hybrid"

        normalized = str(raw_type).strip().lower().replace("_", "-")
        type_map = {
            "grub2-hybrid": "grub2-hybrid",
            "hybrid": "grub2-hybrid",
            "grub2-uefi": "grub2-uefi",
            "uefi": "grub2-uefi",
            "efi": "grub2-uefi",
            "grub2-bios": "grub2-bios",
            "bios": "grub2-bios",
            "syslinux": "syslinux",
            "isolinux": "syslinux",
        }
        return type_map.get(normalized, "grub2-hybrid")

    def should_use_grub_efi(self) -> bool:
        return self.get_bootloader_type() in {"grub2-hybrid", "grub2-uefi"}

    def should_use_grub_bios(self) -> bool:
        return self.get_bootloader_type() in {"grub2-hybrid", "grub2-bios"}

    def should_use_syslinux(self) -> bool:
        return self.get_bootloader_type() == "syslinux"

    def _get_iso_label(self) -> str:
        raw_label = self.config.get("iso_label", self.config.get("system", {}).get("iso_label", "DEBIAN_MODERN"))
        sanitized = re.sub(r"[^A-Z0-9_]+", "_", raw_label.upper().strip())
        sanitized = sanitized.strip("_")
        return (sanitized or "DEBIAN_MODERN")[:32]

    def _get_kernel_params(self) -> str:
        # Start from config or use a clean default — never embed quiet/splash so
        # boot errors are always visible. The caller can add them back later.
        base = self.config.get("kernel_params", self.config.get("boot", {}).get("kernel_params", ""))
        # Strip out quiet/splash regardless of where they came from
        base = " ".join(p for p in base.split() if p not in ("quiet", "splash"))
        if "boot=live" not in base:
            base = f"boot=live {base}"
        if "components" not in base:
            base = f"{base} components"
        if "union=overlay" not in base:
            base = f"{base} union=overlay"
        return base.strip()

    def _get_template_placeholders(self) -> Dict[str, str]:
        iso_label = self._get_iso_label()
        kernel_params = self._get_kernel_params()
        desktop = str(self.config.get("desktop", "xfce")).upper()
        distro = str(self.config.get("distro", "Debian")).title()
        arch = self.arch
        keymap = self.config.get("keymap", "us")
        locale = self.config.get("locale", "en_US.UTF-8")
        live_user = self.config.get("live_user", "liveuser")
        if isinstance(live_user, dict):
            live_user = live_user.get("name", "liveuser")

        return {
            "@@VOL_ID@@": iso_label,
            "@@ISO_LABEL@@": iso_label,
            "@@BOOT_TITLE@@": f"{distro} Modern",
            "@@DISTRO_NAME@@": f"{distro} Modern",
            "@@DESKTOP@@": desktop,
            "@@ARCH@@": arch,
            "@@KERNEL_PARAMS@@": kernel_params,
            "@@BOOT_CMDLINE@@": kernel_params,
            "@@KEYMAP@@": keymap,
            "@@LOCALE@@": locale,
            "@@LIVE_USER@@": live_user,
            "@@SPLASHIMAGE@@": "splash.png"
        }

    def _find_kernel_and_initramfs(self) -> Tuple[str, str]:
        boot_dir = self.target_root / "boot"
        kernel = None
        initramfs = None

        if boot_dir.exists():
            vmlinuz_files = sorted([f.name for f in boot_dir.glob("vmlinuz-*") if not f.name.endswith(".old") and not f.name.endswith(".bak")])
            initrd_files = sorted([f.name for f in boot_dir.glob("initrd.img-*") if not f.name.endswith(".old") and not f.name.endswith(".bak")])
            if vmlinuz_files:
                kernel = vmlinuz_files[-1]
            if initrd_files:
                initramfs = initrd_files[-1]

        return kernel or "vmlinuz", initramfs or "initrd.img"

    def _create_squashfs(self, source_dir: Path, output_path: Path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            output_path.touch()
            return

        if output_path.exists():
            output_path.unlink()

        # Ensure essential mountpoint directories exist in rootfs before creating squashfs
        for d in ["proc", "sys", "dev", "tmp", "run", "mnt", "media", "var/cache/apt"]:
            (source_dir / d).mkdir(parents=True, exist_ok=True)

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
                "-e", "proc/*", "sys/*", "dev/*", "tmp/*", "run/*", "var/cache/apt/*"
            ],
        )

    def generate_grub_bios_core(self) -> Path:
        eltorito_img = self.iso_staging / "boot" / "grub" / "grub_eltorito"
        eltorito_img.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            eltorito_img.touch()
            return eltorito_img

        # 1. Search for i386-pc directory with cdboot.img (exact live-build architecture)
        search_roots = [
            self.target_root,
            self.workdir / "build_host",
            Path("/")
        ]
        i386_dir = None
        for r in search_roots:
            for candidate in [
                r / "usr" / "lib" / "grub" / "i386-pc",
                r / "usr" / "lib" / "grub2" / "i386-pc"
            ]:
                if candidate.exists() and (candidate / "cdboot.img").exists():
                    i386_dir = candidate
                    break
            if i386_dir:
                break

        if i386_dir:
            # Copy all GRUB modules to /boot/grub/i386-pc (exact Debian live-build architecture)
            target_i386_dir = self.iso_staging / "boot" / "grub" / "i386-pc"
            target_i386_dir.mkdir(parents=True, exist_ok=True)
            for item in i386_dir.glob("*"):
                if item.is_file() and item.suffix in [".mod", ".lst", ".pf2"]:
                    shutil.copy2(item, target_i386_dir / item.name)

            early_cfg = self.workdir / "early-bios-grub.cfg"
            early_cfg.write_text(
                "insmod search\n"
                "insmod search_fs_file\n"
                "insmod search_label\n"
                "insmod iso9660\n"
                "insmod fat\n"
                "insmod part_gpt\n"
                "insmod part_msdos\n"
                "insmod test\n"
                f"search --no-floppy --set=root --label {self._get_iso_label()}\n"
                "if [ -z \"$root\" ]; then\n"
                "    search --no-floppy --set=root --file /.disk/info\n"
                "fi\n"
                "if [ -z \"$root\" ]; then\n"
                "    search --no-floppy --set=root --file /live/vmlinuz\n"
                "fi\n"
                "if [ -z \"$root\" ]; then\n"
                "    search --no-floppy --set=root --file /install/vmlinuz\n"
                "fi\n"
                "set prefix=($root)/boot/grub\n"
                "configfile ($root)/boot/grub/grub.cfg\n"
            )

            core_tmp = self.workdir / "core_tmp.img"
            if core_tmp.exists():
                core_tmp.unlink()

            res = self.toolchain.run_tool(
                "grub-mkimage",
                [
                    "-d", str(i386_dir),
                    "-c", str(early_cfg),
                    "-o", str(core_tmp),
                    "-O", "i386-pc",
                    "--prefix=/boot/grub",
                    "biosdisk", "iso9660", "search", "search_fs_file", "search_label", "configfile", "normal", "linux", "gzio", "part_gpt", "part_msdos", "fat", "ext2", "test", "echo", "loadenv", "all_video", "gfxterm", "font", "gettext", "png", "terminal"
                ],
                check=False
            )
            if res.returncode == 0 and core_tmp.exists() and core_tmp.stat().st_size > 0:
                cdboot_path = i386_dir / "cdboot.img"
                with open(eltorito_img, "wb") as f_out:
                    f_out.write(cdboot_path.read_bytes())
                    f_out.write(core_tmp.read_bytes())
                logger.info(f"Successfully generated live-build GRUB BIOS image: {eltorito_img} ({eltorito_img.stat().st_size} bytes)")
                return eltorito_img

        # 2. Fallback: Standalone GRUB BIOS image via grub-mkstandalone
        embedded_cfg = self.workdir / "early-grub.cfg"
        embedded_cfg.write_text(
            "search --no-floppy --set=root --file /live/vmlinuz\n"
            "set prefix=($root)/boot/grub\n"
        )
        res = self.toolchain.run_tool(
            "grub-mkstandalone",
            [
                "--format=i386-pc",
                "-o", str(eltorito_img),
                f"boot/grub/grub.cfg={embedded_cfg}",
                "--install-modules=iso9660 search search_fs_file search_label configfile normal biosdisk part_msdos part_gpt linux",
                "--fonts=", "--locales=", "--themes="
            ],
            check=False
        )
        if res.returncode == 0 and eltorito_img.exists() and eltorito_img.stat().st_size > 0:
            logger.info(f"Successfully compiled standalone GRUB BIOS image: {eltorito_img} ({eltorito_img.stat().st_size} bytes)")
            return eltorito_img

        logger.warning("Could not generate BIOS bootloader image.")
        return eltorito_img

    def generate_grub_efi_image(self):
        efiboot_img = self.iso_staging / "boot" / "grub" / "efiboot.img"
        efiboot_img.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            efiboot_img.touch()
            return

        iso_efi_dir = self.iso_staging / "EFI" / "BOOT"
        iso_efi_dir.mkdir(parents=True, exist_ok=True)

        embedded_cfg = (
            "insmod iso9660\n"
            "insmod fat\n"
            "insmod part_gpt\n"
            "insmod part_msdos\n"
            "insmod search\n"
            "insmod search_fs_file\n"
            "insmod normal\n"
            "search --file --set=root /.disk/info\n"
            "if [ -z \"$root\" ]; then search --file --set=root /live/vmlinuz; fi\n"
            "if [ -z \"$root\" ]; then search --file --set=root /boot/grub/grub.cfg; fi\n"
            "if [ -z \"$root\" ]; then set root=cd0; fi\n"
            "set prefix=($root)/boot/grub\n"
            "configfile ($root)/boot/grub/grub.cfg\n"
            "configfile ($root)/EFI/BOOT/grub.cfg\n"
        )

        # Copy EFI modules to /boot/grub/<platform> on ISO staging (exact live-build requirement)
        search_roots = [
            self.target_root,
            self.workdir / "build_host",
            Path("/")
        ]
        for efi_platform in ["x86_64-efi", "i386-efi"]:
            for r in search_roots:
                src_efi_dir = r / "usr" / "lib" / "grub" / efi_platform
                if src_efi_dir.exists():
                    dst_efi_dir = self.iso_staging / "boot" / "grub" / efi_platform
                    dst_efi_dir.mkdir(parents=True, exist_ok=True)
                    for item in src_efi_dir.glob("*"):
                        if item.is_file() and item.suffix in [".mod", ".lst", ".pf2"]:
                            shutil.copy2(item, dst_efi_dir / item.name)
                    # Create the platform-specific grub.cfg that configures root and loads main grub.cfg
                    iso_label = self._get_iso_label()
                    (dst_efi_dir / "grub.cfg").write_text(
                        "insmod efidisk\n"
                        "insmod part_gpt\n"
                        "insmod part_msdos\n"
                        "insmod fat\n"
                        "insmod iso9660\n"
                        "insmod search\n"
                        "insmod search_fs_file\n"
                        "insmod search_label\n"
                        "insmod test\n"
                        f"search --no-floppy --set=root --label {iso_label}\n"
                        "if [ -z \"$root\" ]; then\n"
                        "    search --no-floppy --set=root --file /.disk/info\n"
                        "fi\n"
                        "if [ -z \"$root\" ]; then\n"
                        "    search --no-floppy --set=root --file /live/vmlinuz\n"
                        "fi\n"
                        "set prefix=($root)/boot/grub\n"
                        "configfile ($root)/boot/grub/grub.cfg\n"
                    )
                    break

        for fmt, boot_filename in [("x86_64-efi", "BOOTX64.EFI"), ("i386-efi", "BOOTIA32.EFI")]:
            mod_dir = None
            for r in search_roots:
                candidate = r / "usr" / "lib" / "grub" / fmt
                if candidate.exists() and (candidate / "modinfo.sh").exists():
                    mod_dir = candidate
                    break

            if not mod_dir:
                logger.debug(f"GRUB modules for {fmt} not found; skipping {boot_filename}")
                continue

            # Write the embedded early config for this specific platform
            iso_label = self._get_iso_label()
            early_cfg = self.workdir / f"early-efi-grub-{fmt}.cfg"
            embedded_cfg = (
                "insmod efidisk\n"
                "insmod part_gpt\n"
                "insmod part_msdos\n"
                "insmod fat\n"
                "insmod iso9660\n"
                "insmod search\n"
                "insmod search_fs_file\n"
                "insmod search_label\n"
                "insmod normal\n"
                "insmod test\n"
                f"search --no-floppy --set=root --label {iso_label}\n"
                "if [ -z \"$root\" ]; then\n"
                "    search --no-floppy --set=root --file /.disk/info\n"
                "fi\n"
                "if [ -z \"$root\" ]; then\n"
                "    search --no-floppy --set=root --file /live/vmlinuz\n"
                "fi\n"
                "if [ -z \"$root\" ]; then\n"
                "    search --no-floppy --set=root --file /boot/grub/grub.cfg\n"
                "fi\n"
                "set prefix=($root)/boot/grub\n"
                "configfile ($root)/boot/grub/grub.cfg\n"
            )
            early_cfg.write_text(embedded_cfg)

            out_binary = iso_efi_dir / boot_filename
            self.toolchain.run_tool(
                "grub-mkstandalone",
                [
                    f"--format={fmt}",
                    "-d", str(mod_dir),
                    "-o", str(out_binary),
                    f"boot/grub/grub.cfg={early_cfg}"
                ],
                check=False
            )

        # Write EFI/BOOT/grub.cfg as well for standalone EFI loaders
        iso_label = self._get_iso_label()
        (iso_efi_dir / "grub.cfg").write_text(
            "insmod efidisk\n"
            "insmod part_gpt\n"
            "insmod part_msdos\n"
            "insmod fat\n"
            "insmod iso9660\n"
            "insmod search\n"
            "insmod search_fs_file\n"
            "insmod search_label\n"
            "insmod normal\n"
            "insmod test\n"
            f"search --no-floppy --set=root --label {iso_label}\n"
            "if [ -z \"$root\" ]; then\n"
            "    search --no-floppy --set=root --file /.disk/info\n"
            "fi\n"
            "if [ -z \"$root\" ]; then\n"
            "    search --no-floppy --set=root --file /live/vmlinuz\n"
            "fi\n"
            "set prefix=($root)/boot/grub\n"
            "configfile ($root)/boot/grub/grub.cfg\n"
        )

        # Also copy signed Shim / GRUB binaries if present for Secure Boot compatibility
        for r in search_roots:
            shim_candidate = r / "usr" / "lib" / "shim" / "shimx64.efi.signed"
            grub_candidate = r / "usr" / "lib" / "grub" / "x86_64-efi-signed" / "gcdx64.efi.signed"
            if shim_candidate.exists() and grub_candidate.exists():
                shutil.copy2(shim_candidate, iso_efi_dir / "shimx64.efi")
                shutil.copy2(shim_candidate, iso_efi_dir / "BOOTX64.EFI")
                shutil.copy2(grub_candidate, iso_efi_dir / "grubx64.efi")
                break

        efi_files = [f for f in iso_efi_dir.glob("*") if f.is_file()]
        if efi_files:
            total_size_mb = max(32, sum(f.stat().st_size for f in efi_files) // (1024 * 1024) + 16)
            size_kb = total_size_mb * 1024

            if efiboot_img.exists():
                efiboot_img.unlink()

            mkfs_res = self.toolchain.run_tool("mkfs.vfat", ["-C", str(efiboot_img), str(size_kb)], check=False)
            if mkfs_res.returncode != 0:
                self.toolchain.run_tool("truncate", ["-s", f"{total_size_mb}M", str(efiboot_img)], check=True)
                self.toolchain.run_tool("mformat", ["-i", str(efiboot_img), "-h", "32", "-t", "32", "-n", "64", "-c", "1", "::"], check=False)

            self.toolchain.run_tool("mmd", ["-i", str(efiboot_img), "::/EFI"], check=False)
            self.toolchain.run_tool("mmd", ["-i", str(efiboot_img), "::/EFI/BOOT"], check=False)
            for f in efi_files:
                self.toolchain.run_tool("mcopy", ["-i", str(efiboot_img), str(f), f"::/EFI/BOOT/{f.name}"], check=False)

    def _copy_syslinux_binaries(self):
        syslinux_paths = [
            self.target_root / "usr" / "lib" / "ISOLINUX",
            self.target_root / "usr" / "lib" / "syslinux" / "modules" / "bios",
            self.target_root / "usr" / "share" / "syslinux",
            self.target_root / "usr" / "lib" / "syslinux" / "bios",
            self.target_root / "usr" / "lib" / "syslinux",
            self.workdir / "build_host" / "usr" / "lib" / "ISOLINUX",
            self.workdir / "build_host" / "usr" / "lib" / "syslinux" / "modules" / "bios",
            self.workdir / "build_host" / "usr" / "share" / "syslinux",
            self.workdir / "build_host" / "usr" / "lib" / "syslinux" / "bios",
            Path("/usr/lib/ISOLINUX"),
            Path("/usr/lib/syslinux/modules/bios"),
            Path("/usr/share/syslinux"),
            Path("/usr/lib/syslinux/bios"),
            Path("/usr/lib/syslinux"),
        ]

        isolinux_target = self.iso_staging / "isolinux"

        sys_files = ["isolinux.bin", "vesamenu.c32", "ldlinux.c32", "libcom32.c32", "libcom.c32", "libutil.c32", "chain.c32", "reboot.c32", "poweroff.c32", "menu.c32"]
        for sys_file in sys_files:
            for path in syslinux_paths:
                src_file = path / sys_file
                if src_file.exists():
                    isolinux_target.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, isolinux_target / sys_file)
                    break

        iso_bin = isolinux_target / "isolinux.bin"
        if iso_bin.exists() and iso_bin.stat().st_size > 0:
            isolinux_template = resolve_from_project("configs/bootloaders/templates/isolinux.cfg.in")
            placeholders = self._get_template_placeholders()
            if isolinux_template.exists():
                syslinux_cfg = isolinux_template.read_text()
                for k, v in placeholders.items():
                    syslinux_cfg = syslinux_cfg.replace(k, str(v))
            else:
                iso_label = self._get_iso_label()
                kernel_params = self._get_kernel_params()
                syslinux_cfg = (
                    "UI vesamenu.c32\n"
                    "PROMPT 0\n"
                    "TIMEOUT 50\n\n"
                    f"LABEL live\n"
                    f"  MENU LABEL ^{iso_label} Live (Standard)\n"
                    f"  MENU DEFAULT\n"
                    f"  KERNEL /live/vmlinuz\n"
                    f"  APPEND initrd=/live/initrd.img {kernel_params}\n\n"
                    f"LABEL live-toram\n"
                    f"  MENU LABEL {iso_label} Live (^Copy to RAM)\n"
                    f"  KERNEL /live/vmlinuz\n"
                    f"  APPEND initrd=/live/initrd.img {kernel_params} toram\n\n"
                    f"LABEL live-persistence\n"
                    f"  MENU LABEL {iso_label} Live (with ^Persistence)\n"
                    f"  KERNEL /live/vmlinuz\n"
                    f"  APPEND initrd=/live/initrd.img {kernel_params} persistence\n\n"
                    f"LABEL failsafe\n"
                    f"  MENU LABEL {iso_label} Live (^Failsafe Mode)\n"
                    f"  KERNEL /live/vmlinuz\n"
                    f"  APPEND initrd=/live/initrd.img {kernel_params} nomodeset xci586 noapic acpi=off\n"
                )
            if self.config.get("with_debian_installer"):
                syslinux_cfg += (
                    "\nLABEL install\n"
                    "  MENU LABEL ^Install (Modo Texto)\n"
                    "  KERNEL /install/vmlinuz\n"
                    "  APPEND initrd=/install/initrd.gz vga=788 --- quiet\n\n"
                    "LABEL install-gtk\n"
                    "  MENU LABEL ^Graphical Install (Modo Gráfico)\n"
                    "  KERNEL /install/gtk/vmlinuz\n"
                    "  APPEND initrd=/install/gtk/initrd.gz video=vesa:ywrap,mtrr vga=788 --- quiet\n\n"
                    "LABEL install-auto\n"
                    "  MENU LABEL ^Automated Install (Preseed Server)\n"
                    "  KERNEL /install/vmlinuz\n"
                    "  APPEND initrd=/install/initrd.gz auto=true priority=critical preseed/file=/install/preseed.cfg --- quiet\n"
                )

            (isolinux_target / "isolinux.cfg").write_text(syslinux_cfg)
        elif isolinux_target.exists():
            shutil.rmtree(isolinux_target, ignore_errors=True)

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
        placeholders = self._get_template_placeholders()

        # 1. Load config.cfg from template if available
        config_template = resolve_from_project("configs/bootloaders/templates/config.cfg.in")
        if config_template.exists():
            config_cfg_text = config_template.read_text()
            for k, v in placeholders.items():
                config_cfg_text = config_cfg_text.replace(k, str(v))
        else:
            config_cfg_text = (
                "set default=0\n\n"
                "if [ x$feature_default_font_path = xy ] ; then\n"
                "    font=unicode\n"
                "else\n"
                "    font=$prefix/unicode.pf2\n"
                "fi\n\n"
                "if loadfont $font ; then\n"
                "    set gfxmode=800x600\n"
                "    set gfxpayload=keep\n"
                "    insmod efi_gop\n"
                "    insmod efi_uga\n"
                "    insmod video_bochs\n"
                "    insmod video_cirrus\n"
                "else\n"
                "    set gfxmode=auto\n"
                "    insmod all_video\n"
                "fi\n\n"
                "insmod gfxterm\n"
                "insmod png\n\n"
                "terminal_output gfxterm\n"
            )

        # 2. Load grub.cfg from template if available
        grub_template = resolve_from_project("configs/bootloaders/templates/grub.cfg.in")
        if grub_template.exists():
            grub_cfg_text = grub_template.read_text()
            for k, v in placeholders.items():
                grub_cfg_text = grub_cfg_text.replace(k, str(v))
        else:
            grub_cfg_text = (
                "source /boot/grub/config.cfg\n\n"
                f"menuentry \"{iso_label} Live (Standard)\" --hotkey=l {{\n"
                f"    linux /live/vmlinuz {kernel_params}\n"
                "    initrd /live/initrd.img\n"
                "}\n\n"
                f"menuentry \"{iso_label} Live (Copy to RAM)\" --hotkey=r {{\n"
                f"    linux /live/vmlinuz {kernel_params} toram\n"
                "    initrd /live/initrd.img\n"
                "}\n\n"
                f"menuentry \"{iso_label} Live (with Persistence)\" --hotkey=p {{\n"
                f"    linux /live/vmlinuz {kernel_params} persistence\n"
                "    initrd /live/initrd.img\n"
                "}\n\n"
                f"menuentry \"{iso_label} Live (Failsafe Mode)\" --hotkey=f {{\n"
                f"    linux /live/vmlinuz {kernel_params} nomodeset xci586 noapic acpi=off\n"
                "    initrd /live/initrd.img\n"
                "}\n"
            )

        # 3. Load loopback.cfg from template if available
        loopback_template = resolve_from_project("configs/bootloaders/templates/loopback.cfg.in")
        if loopback_template.exists():
            loopback_cfg_text = loopback_template.read_text()
            for k, v in placeholders.items():
                loopback_cfg_text = loopback_cfg_text.replace(k, str(v))
        else:
            loopback_cfg_text = "source /boot/grub/grub.cfg\n"

        if self.config.get("with_debian_installer"):
            install_dir = self.iso_staging / "install"
            install_gtk_dir = install_dir / "gtk"
            install_dir.mkdir(parents=True, exist_ok=True)
            install_gtk_dir.mkdir(parents=True, exist_ok=True)

            vmlinuz_src = self.iso_staging / "live" / "vmlinuz"
            initrd_src = self.iso_staging / "live" / "initrd.img"

            if vmlinuz_src.exists():
                shutil.copy2(vmlinuz_src, install_dir / "vmlinuz")
                shutil.copy2(vmlinuz_src, install_gtk_dir / "vmlinuz")
            else:
                (install_dir / "vmlinuz").touch()
                (install_gtk_dir / "vmlinuz").touch()

            if initrd_src.exists():
                shutil.copy2(initrd_src, install_dir / "initrd.gz")
                shutil.copy2(initrd_src, install_gtk_dir / "initrd.gz")
            else:
                (install_dir / "initrd.gz").touch()
                (install_gtk_dir / "initrd.gz").touch()

            di_custom_dir = resolve_from_project("configs/debian-installer")
            preseed_files = []

            desktop_profile = self.config.get("desktop")
            custom_preseed_arg = self.config.get("preseed")

            if custom_preseed_arg:
                arg_path = Path(custom_preseed_arg)
                if arg_path.exists():
                    preseed_files.append(arg_path)
                elif (di_custom_dir / f"{custom_preseed_arg}.cfg").exists():
                    preseed_files.append(di_custom_dir / f"{custom_preseed_arg}.cfg")
                elif (di_custom_dir / f"preseed-{custom_preseed_arg}.cfg").exists():
                    preseed_files.append(di_custom_dir / f"preseed-{custom_preseed_arg}.cfg")
                elif (di_custom_dir / custom_preseed_arg).exists():
                    preseed_files.append(di_custom_dir / custom_preseed_arg)
            elif desktop_profile and (di_custom_dir / f"preseed-{desktop_profile}.cfg").exists():
                preseed_files.append(di_custom_dir / f"preseed-{desktop_profile}.cfg")
            elif not desktop_profile and (di_custom_dir / "preseed-server.cfg").exists():
                preseed_files.append(di_custom_dir / "preseed-server.cfg")
            elif di_custom_dir.exists():
                preseed_files = sorted([f for f in di_custom_dir.glob("*.cfg") if f.is_file()])

            preseed_blocks = []
            for pf in preseed_files:
                try:
                    preseed_blocks.append(f"# --- Preseed: {pf.name} ---\n" + pf.read_text())
                except Exception as e:
                    logger.warning(f"Could not read preseed file {pf}: {e}")

            has_preseed = len(preseed_blocks) > 0
            preseed_content = "\n".join(preseed_blocks) if has_preseed else ""

            if (di_custom_dir / "early_command.sh").exists():
                shutil.copy2(di_custom_dir / "early_command.sh", install_dir / "early_command.sh")
                try:
                    (install_dir / "early_command.sh").chmod(0o755)
                except Exception:
                    pass
                preseed_content += "\nd-i preseed/early_command string /bin/sh /cdrom/install/early_command.sh\n"

            if (di_custom_dir / "late_command.sh").exists():
                shutil.copy2(di_custom_dir / "late_command.sh", install_dir / "late_command.sh")
                try:
                    (install_dir / "late_command.sh").chmod(0o755)
                except Exception:
                    pass
                preseed_content += "\nd-i preseed/late_command string in-target /bin/sh /cdrom/install/late_command.sh\n"

            if (di_custom_dir / "splash.png").exists():
                shutil.copy2(di_custom_dir / "splash.png", install_gtk_dir / "splash.png")
                preseed_content += "\nd-i debian-installer/splashpng string /cdrom/install/gtk/splash.png\n"

            custom_files_proj = resolve_from_project("configs/custom_files")
            if custom_files_proj.exists() and custom_files_proj.is_dir():
                iso_custom_files = self.iso_staging / "configs" / "custom_files"
                shutil.copytree(custom_files_proj, iso_custom_files, dirs_exist_ok=True)

            if preseed_content.strip():
                (install_dir / "preseed.cfg").write_text(preseed_content)

            grub_cfg_text += (
                "\nmenuentry 'Install (Modo Texto)' {\n"
                "    linux /install/vmlinuz vga=788 --- quiet\n"
                "    initrd /install/initrd.gz\n"
                "}\n\n"
                "menuentry 'Graphical Install (Modo Gráfico)' {\n"
                "    linux /install/gtk/vmlinuz video=vesa:ywrap,mtrr vga=788 --- quiet\n"
                "    initrd /install/gtk/initrd.gz\n"
                "}\n"
            )

            if (install_dir / "preseed.cfg").exists():
                grub_cfg_text += (
                    "\nmenuentry 'Automated Install (Preseed Server)' {\n"
                    "    linux /install/vmlinuz auto=true priority=critical preseed/file=/install/preseed.cfg --- quiet\n"
                    "    initrd /install/initrd.gz\n"
                    "}\n"
                )

        di_mode = self.config.get("di_mode", "live")
        if di_mode == "netboot":
            logger.info("📡 Generating Debian-Installer PXE/TFTP Network Boot Tree...")
            tftp_dir = self.iso_staging / "tftpboot" / "debian-installer" / self.arch
            pxe_cfg_dir = self.iso_staging / "tftpboot" / "pxelinux.cfg"
            tftp_dir.mkdir(parents=True, exist_ok=True)
            pxe_cfg_dir.mkdir(parents=True, exist_ok=True)

            install_vmlinuz = self.iso_staging / "install" / "vmlinuz"
            install_initrd = self.iso_staging / "install" / "initrd.gz"

            if install_vmlinuz.exists():
                shutil.copy2(install_vmlinuz, tftp_dir / "vmlinuz")
            else:
                (tftp_dir / "vmlinuz").touch()

            if install_initrd.exists():
                shutil.copy2(install_initrd, tftp_dir / "initrd.gz")
            else:
                (tftp_dir / "initrd.gz").touch()

            (pxe_cfg_dir / "default").write_text(
                "DEFAULT install\n"
                "PROMPT 0\n"
                "TIMEOUT 50\n\n"
                "LABEL install\n"
                f"  KERNEL debian-installer/{self.arch}/vmlinuz\n"
                f"  APPEND initrd=debian-installer/{self.arch}/initrd.gz vga=788 --- quiet\n"
            )

            netboot_tar = resolve_from_project(f"output/{self.output_name}-netboot-tftp.tar.gz")
            netboot_tar.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["tar", "-czf", str(netboot_tar), "-C", str(self.iso_staging / "tftpboot"), "."], check=False)
            logger.info(f"Successfully generated Netboot TFTP archive: {netboot_tar}")

        # 1. Generate /.disk/info for live-boot media detection BEFORE generating bootloaders
        disk_dir = self.iso_staging / ".disk"
        disk_dir.mkdir(parents=True, exist_ok=True)
        (disk_dir / "info").write_text(f"{iso_label} {self.arch}\n")
        (disk_dir / "release_notes_url").write_text("https://debian.org\n")

        for d in [
            self.iso_staging / "boot" / "grub",
            self.iso_staging / "boot" / "grub2",
            self.iso_staging / "EFI" / "BOOT",
            self.iso_staging / "EFI" / "boot",
        ]:
            d.mkdir(parents=True, exist_ok=True)
            (d / "config.cfg").write_text(config_cfg_text)
            (d / "grub.cfg").write_text(grub_cfg_text)

        (self.iso_staging / "boot" / "grub" / "loopback.cfg").write_text("source /boot/grub/grub.cfg\n")

        # Copy unicode.pf2 font if available
        for font_candidate in [
            self.target_root / "usr" / "share" / "grub" / "unicode.pf2",
            self.workdir / "build_host" / "usr" / "share" / "grub" / "unicode.pf2",
            Path("/usr/share/grub/unicode.pf2"),
        ]:
            if font_candidate.exists():
                shutil.copy2(font_candidate, self.iso_staging / "boot" / "grub" / "unicode.pf2")
                break

        for platform_dir in [self.iso_staging / "boot" / "grub" / "x86_64-efi", self.iso_staging / "boot" / "grub" / "i386-efi"]:
            platform_dir.mkdir(parents=True, exist_ok=True)
            (platform_dir / "grub.cfg").write_text(
                "if [ x$grub_platform == xefi -a x$lockdown != xy ] ; then\n"
                "    insmod part_gpt\n"
                "    insmod part_msdos\n"
                "fi\n"
                "source /boot/grub/grub.cfg\n"
            )

        if self.should_use_syslinux():
            self._copy_syslinux_binaries()
        if self.should_use_grub_bios():
            bios_core = self.generate_grub_bios_core()
        if self.should_use_grub_efi():
            self.generate_grub_efi_image()

        # 2. Generate live/filesystem.packages for Calamares installer (live-build standard)
        try:
            dpkg_status = self.target_root / "var" / "lib" / "dpkg" / "status"
            if dpkg_status.exists():
                pkgs = []
                curr_pkg = None
                curr_ver = None
                for line in dpkg_status.read_text().splitlines():
                    if line.startswith("Package: "):
                        curr_pkg = line.split(": ", 1)[1].strip()
                    elif line.startswith("Version: "):
                        curr_ver = line.split(": ", 1)[1].strip()
                    elif line == "" and curr_pkg and curr_ver:
                        pkgs.append(f"{curr_pkg}\t{curr_ver}")
                        curr_pkg, curr_ver = None, None
                if pkgs:
                    (self.iso_staging / "live" / "filesystem.packages").write_text("\n".join(pkgs) + "\n")
        except Exception as e:
            logger.warning(f"Could not write filesystem.packages manifest: {e}")

        # 3. Generate md5sum.txt inside ISO root (live-build binary_checksums standard)
        try:
            md5_lines = []
            for p in sorted(self.iso_staging.rglob("*")):
                if p.is_file() and p.name != "md5sum.txt" and not str(p.relative_to(self.iso_staging)).startswith("isolinux"):
                    rel = p.relative_to(self.iso_staging)
                    h = hashlib.md5(p.read_bytes()).hexdigest()
                    md5_lines.append(f"{h}  ./{rel}")
            if md5_lines:
                (self.iso_staging / "md5sum.txt").write_text("\n".join(md5_lines) + "\n")
        except Exception as e:
            logger.warning(f"Could not write ISO md5sum.txt: {e}")

        iso_path = resolve_from_project(f"output/{self.output_name}.iso")
        iso_path.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            iso_path.touch()
        else:
            search_roots = [
                self.target_root,
                self.workdir / "build_host",
                Path("/")
            ]

            mbr_isohdpfx = None
            mbr_grub = None
            for r in search_roots:
                for candidate in [
                    r / "usr" / "lib" / "ISOLINUX" / "isohdpfx.bin",
                    r / "usr" / "lib" / "syslinux" / "isohdpfx.bin",
                    r / "usr" / "lib" / "syslinux" / "modules" / "bios" / "isohdpfx.bin",
                ]:
                    if candidate.exists() and candidate.stat().st_size > 0:
                        mbr_isohdpfx = candidate
                        break
                for candidate in [
                    r / "usr" / "lib" / "grub" / "i386-pc" / "boot_hybrid.img",
                    r / "usr" / "lib" / "grub2" / "i386-pc" / "boot_hybrid.img",
                ]:
                    if candidate.exists() and candidate.stat().st_size > 0:
                        mbr_grub = candidate
                        break

            # Copy offline repository into ISO staging if available
            offline_repo_dir = self.config.get("offline_repo_dir")
            if offline_repo_dir and Path(offline_repo_dir).exists():
                target_repo = self.iso_staging / "repo"
                target_repo.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(offline_repo_dir, target_repo, dirs_exist_ok=True, symlinks=True, ignore_dangling_symlinks=True)

            xorriso_args = [
                "-as", "mkisofs",
                "-V", iso_label,
                "-r", "-J", "-joliet-long", "-cache-inodes",
                "-pad",
            ]

            isolinux_bin = self.iso_staging / "isolinux" / "isolinux.bin"
            grub_eltorito = self.iso_staging / "boot" / "grub" / "grub_eltorito"
            efiboot_img = self.iso_staging / "boot" / "grub" / "efiboot.img"

            if self.should_use_syslinux() and isolinux_bin.exists() and isolinux_bin.stat().st_size > 0:
                if mbr_isohdpfx:
                    xorriso_args.extend(["-isohybrid-mbr", str(mbr_isohdpfx), "-partition_offset", "16"])
                elif mbr_grub:
                    xorriso_args.extend(["-isohybrid-mbr", str(mbr_grub)])

                xorriso_args.extend([
                    "-b", "isolinux/isolinux.bin",
                    "-c", "isolinux/boot.cat",
                    "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table",
                ])
            elif self.should_use_grub_bios() and grub_eltorito.exists() and grub_eltorito.stat().st_size > 0:
                if mbr_grub:
                    xorriso_args.extend(["--grub2-boot-info", "--grub2-mbr", str(mbr_grub)])
                elif mbr_isohdpfx:
                    xorriso_args.extend(["-isohybrid-mbr", str(mbr_isohdpfx)])

                xorriso_args.extend([
                    "-b", "boot/grub/grub_eltorito",
                    "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table",
                ])

            if self.should_use_grub_efi() and efiboot_img.exists() and efiboot_img.stat().st_size > 0:
                xorriso_args.extend([
                    "-eltorito-alt-boot",
                    "-e", "boot/grub/efiboot.img",
                    "-no-emul-boot",
                    "-isohybrid-gpt-basdat",
                    "-append_partition", "2", "0xef", str(efiboot_img)
                ])

            xorriso_args.extend([
                "-o", str(iso_path),
                str(self.iso_staging)
            ])

            logger.info("Executing xorriso to create live-build compliant ISO: %s", " ".join(xorriso_args))
            self.toolchain.run_tool("xorriso", xorriso_args, check=False)

        return iso_path

    def build_tarball(self) -> Path:
        tar_path = resolve_from_project(f"output/stage3_seeds/{self.output_name}.tar.xz")
        tar_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            tar_path.touch()
        else:
            subprocess.run([
                "tar", "cJpf", str(tar_path),
                "--exclude=./proc/*", "--exclude=./sys/*", "--exclude=./dev/*",
                "--exclude=./tmp/*", "--exclude=./run/*",
                "-C", str(self.target_root), "."
            ], check=True)
        return tar_path
