import os
import re
import shutil
import subprocess
import time
import hashlib
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Tuple, Any
import logging
from deb_dev_builder.core.runtime_config import load_runtime, architecture_settings, live_user_settings
from deb_dev_builder.core.toolchain_manager import ToolchainManager
from deb_dev_builder.core.path_utils import resolve_from_project, resolve_output_path
from deb_dev_builder.core.installer_media import LiveInstallerMedia
from deb_dev_builder.core.initrd_editor import Entry, archive_path, customize_initrd
from deb_dev_builder.core.build_report import file_digest
from deb_dev_builder.core.package_policy import installed_packages
from .installer_autologin import autologin_entries
from .installer_locale import locale_entries
from .installer_grub import grub_entries
import fnmatch
import gzip

logger = logging.getLogger("iso_engine")

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
        self.runtime = load_runtime(config)
        self.arch = config.get("architecture", self.runtime["cli"]["architecture"])
        self.arch_policy = architecture_settings(config, self.runtime)

    def _resolve_output_path(self, extension: str) -> Path:
        return resolve_output_path(self.output_name, extension)

    def _write_tar_archive(self, source, output):
        """Publish a complete archive only after closing both tar and gzip."""
        epoch = self.runtime["build"]["reproducibility"]["source_date_epoch"]
        def metadata(info):
            if epoch is not None:
                info.mtime = epoch
                info.uname = info.gname = ""
            return info
        with tempfile.TemporaryDirectory(prefix=".archive-", dir=output.parent) as directory:
            pending = Path(directory) / output.name
            with pending.open("wb") as stream, gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=epoch) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as result:
                    for path in sorted(source.rglob("*")):
                        result.add(path, arcname=path.relative_to(source), recursive=False, filter=metadata)
            with tarfile.open(pending, "r:gz") as check:
                check.getmembers()
            os.replace(pending, output)

    def _publish_iso(self, iso_path: Path, xorriso_args: list[str]):
        # Never truncate an ISO that a running VM may still have open. Keeping
        # staging beside the destination makes replacement atomic on one FS.
        with tempfile.TemporaryDirectory(prefix=f".{iso_path.stem}-", dir=iso_path.parent) as directory:
            pending = Path(directory) / iso_path.name
            epoch = self.runtime["build"]["reproducibility"]["source_date_epoch"]
            if epoch is not None:
                for path in [*self.iso_staging.rglob("*"), self.iso_staging]:
                    os.utime(path, (epoch, epoch), follow_symlinks=False)
                timestamp = time.strftime("%Y%m%d%H%M%S00", time.gmtime(epoch))
                xorriso_args = [*xorriso_args, "--modification-date=" + timestamp]
            args = [*xorriso_args, "-o", str(pending), str(self.iso_staging)]
            logger.info("Executing xorriso to create ISO: %s", " ".join(args))
            self.toolchain.run_tool("xorriso", args, check=True)
            if not pending.is_file() or pending.stat().st_size == 0:
                raise ISOEngineError(f"xorriso did not create a usable ISO: {pending}")
            os.replace(pending, iso_path)

    def _build_host_root(self) -> Path:
        return Path(getattr(self.toolchain, "build_host_dir", self.workdir.parent / "build_host"))

    def get_bootloader_type(self) -> str:
        bootloader = self.config.get("bootloader", {})
        if isinstance(bootloader, str):
            raw_type = bootloader
        else:
            raw_type = bootloader.get("type") if isinstance(bootloader, dict) else None
        if not raw_type:
            raw_type = self.config.get("bootloader_type") or self.config.get("boot", {}).get("type") or self.runtime["build"]["iso_bios_bootloader"]

        normalized = str(raw_type).strip().lower().replace("_", "-")
        return self.runtime["build"]["bootloader_aliases"].get(normalized, self.runtime["build"]["iso_bios_bootloader"])

    def should_use_grub_efi(self) -> bool:
        return self.get_bootloader_type() in {"grub2-hybrid", "grub2-uefi"}

    def should_use_grub_bios(self) -> bool:
        return self.arch_policy["bios"] and self.get_bootloader_type() in {"grub2-hybrid", "grub2-bios"}

    def should_use_syslinux(self) -> bool:
        return self.get_bootloader_type() == "syslinux"

    def _get_iso_label(self) -> str:
        raw_label = self.config.get("iso_label", self.config.get("system", {}).get("iso_label", self.runtime["system"]["iso_label"]))
        sanitized = re.sub(r"[^A-Z0-9_]+", "_", raw_label.upper().strip())
        sanitized = sanitized.strip("_")
        return (sanitized or self.runtime["system"]["iso_label"])[:32]

    def _get_kernel_params(self) -> str:
        # Activate graphical boot only when the target contains Plymouth/theme.
        base = self.config.get("kernel_params", self.config.get("boot", {}).get("kernel_params", self.runtime["artifacts"]["iso"]["kernel_params"]))
        policy = self.runtime["artifacts"]["iso"]
        base = " ".join(p for p in base.split() if p not in policy["hidden_kernel_options"])
        plymouth = self.runtime["customization"]["plymouth"]
        if (self.target_root / plymouth["binary"]).is_file() and (self.target_root / plymouth["directory"] / (plymouth["name"] + ".plymouth")).is_file():
            for option in policy["plymouth_kernel_options"]:
                if option not in base.split():
                    base = f"{base} {option}"
        for option in policy["required_kernel_options"]:
            if option not in base.split():
                base = f"{base} {option}"
        return base.strip()

    def _get_template_placeholders(self) -> Dict[str, str]:
        iso_label = self._get_iso_label()
        kernel_params = self._get_kernel_params()
        desktop = str(self.config.get("desktop", self.runtime["desktop"]["fallback_session"])).upper()
        distro = str(self.config.get("distro_name", self.config.get("distro", self.runtime["system"]["base_distro"].capitalize())))
        arch = self.arch
        keymap = self.config.get("keymap", self.runtime["artifacts"]["iso"]["keymap"])
        locale = self.config.get("locale", self.config.get("system", {}).get("locale", self.runtime["system"]["locale"]))
        live_user = live_user_settings(self.config, self.runtime)["name"]

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

    def _installer_image_url(self, relative: str = "") -> str:
        from .installer_source import installer_mirror, daily_installer
        policy = self.runtime["installer"]
        if self.config.get("base_distro") not in policy["distributions"]:
            raise ISOEngineError("Native installer artifacts require a Debian or Devuan distro profile")
        mirror = installer_mirror(self.config, policy, images=True)
        suite = self.config.get("suite")
        arch = self.config.get("dpkg_arch", self.arch)
        if not suite or not arch:
            raise ISOEngineError("Debian Installer requires suite and dpkg_arch configuration")
        source = "netboot" if relative == "netboot.tar.gz" else policy["sources"][self.config.get("di_mode", "live")]
        daily = daily_installer(self.config, policy)
        if daily and not (self.config.get("installer_image_mirror") or policy["image_mirror"] or self.config.get("installer_mirror")):
            base = daily["url"].format(architecture=arch).rstrip('/') + '/' + source
        else:
            base = f'{mirror}/{policy["images_path"].format(suite=suite, architecture=arch)}/{source}'
        return f"{base}/{relative.lstrip('/')}" if relative else base

    def _download_installer_file(self, relative: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(self._installer_image_url(relative), timeout=self.runtime["installer"]["download_timeout"]) as response:
                with destination.open("wb") as output:
                    shutil.copyfileobj(response, output)
        except (OSError, urllib.error.URLError) as exc:
            raise ISOEngineError(f"Could not download native d-i artifact {relative}: {exc}") from exc
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ISOEngineError(f"Downloaded native d-i artifact is empty: {relative}")

    def prepare_installer_bundle(self):
        """Freeze native live-installer inputs before rootfs work or compression."""
        if (self.mode == 'mock' or not self.config.get('with_debian_installer')
                or self.config.get('di_mode', 'live') != 'live' or not self.runtime['installer']['bundle']['enabled']):
            return
        from .installer_bundle import InstallerBundleResolver, load_bundle, digest
        resolver = InstallerBundleResolver(self.config, self.runtime['installer'], self._installer_image_url('').rsplit('/', 1)[0])
        try:
            pinned = self.config.get('_installer_bundle')
            path = Path(pinned) if pinned else resolver.resolve()
            manifest = load_bundle(path, resolver.identity)
        except (OSError, ValueError, KeyError) as exc:
            raise ISOEngineError(f'Could not freeze native live-installer inputs: {exc}') from exc
        self.config['_installer_bundle'] = str(path)
        self.config['installer_bundle'] = {
            'id': path.parent.name, 'kernel': manifest['kernel'],
            'image_base': manifest['image_base'], 'image_date': manifest['image_date'],
            'repository': manifest['repository'], 'manifest_sha256': digest(path.read_bytes()),
            'index_sha256': manifest['files']['Packages'],
        }

    def _stage_debian_installer(self, install_dir: Path) -> None:
        """Stage genuine d-i text and GTK kernels/initrds for the selected suite."""
        arch = self.config.get("dpkg_arch", self.arch)
        policy = self.runtime["installer"]
        source = policy["sources"][self.config.get("di_mode", "live")]
        targets = {key: value.format(architecture=arch) for key, value in policy["targets"][source].items()}
        install_gtk_dir = install_dir / "gtk"
        install_dir.mkdir(parents=True, exist_ok=True)
        install_gtk_dir.mkdir(parents=True, exist_ok=True)
        if self.config.get('_installer_bundle'):
            from .installer_bundle import load_bundle
            manifest_path = Path(self.config['_installer_bundle'])
            bundle = load_bundle(manifest_path)
            destinations = {'kernel': install_dir / 'vmlinuz', 'initrd': install_dir / 'initrd.gz',
                            'gtk_kernel': install_gtk_dir / 'vmlinuz', 'gtk_initrd': install_gtk_dir / 'initrd.gz'}
            for key, local in bundle['images'].items():
                shutil.copy2(manifest_path.parent / local, destinations[key])
            self.config['di_graphical'] = 'gtk_initrd' in bundle['images']
            if not self.config['di_graphical']:
                shutil.rmtree(install_gtk_dir)
            return
        text_targets = (
            (targets["kernel"], install_dir / "vmlinuz"),
            (targets["initrd"], install_dir / "initrd.gz"),
        )
        graphical_targets = (
            (targets["gtk_kernel"], install_gtk_dir / "vmlinuz"),
            (targets["gtk_initrd"], install_gtk_dir / "initrd.gz"),
        )
        if self.mode == "mock":
            for _, destination in text_targets + graphical_targets:
                destination.touch()
            self.config["di_graphical"] = True
            return
        for relative, destination in text_targets:
            self._download_installer_file(relative, destination)
        try:
            for relative, destination in graphical_targets:
                self._download_installer_file(relative, destination)
            self.config["di_graphical"] = True
        except ISOEngineError as exc:
            logger.warning("Graphical d-i is unavailable for %s: %s", arch, exc)
            shutil.rmtree(install_gtk_dir, ignore_errors=True)
            self.config["di_graphical"] = False

    def _resolve_preseed_files(self):
        preseed_dir = resolve_from_project("configs/debian-installer")
        requested = self.config.get("preseed")
        desktop = self.config.get("desktop")
        candidates = []
        if requested:
            requested_path = Path(requested)
            candidates = [
                requested_path,
                preseed_dir / f"{requested}.cfg",
                preseed_dir / f"preseed-{requested}.cfg",
                preseed_dir / str(requested),
            ]
        elif self.config.get("di_mode", "live") == "live":
            # Desktop preseed profiles select network packages; live installs
            # already contain the selected desktop and customizations.
            return []
        elif desktop:
            candidates = [preseed_dir / f"preseed-{desktop}.cfg"]
        else:
            candidates = [preseed_dir / "preseed-server.cfg", preseed_dir / "preseed.cfg"]
        return [path for path in candidates if path.is_file()][:1]

    def build_netboot_archive(self) -> Path:
        """Build a complete official d-i PXE tree with optional local preseed files."""
        output = self._resolve_output_path(".netboot.tar.gz")
        output.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            output.touch()
            return output

        staging = self.workdir / "netboot_root"
        archive = self.workdir / "debian-installer-netboot.tar.gz"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        self._download_installer_file("netboot.tar.gz", archive)
        try:
            with tarfile.open(archive, "r:gz") as source:
                source.extractall(staging, filter="data")
        except (OSError, tarfile.TarError) as exc:
            raise ISOEngineError(f"Invalid Debian Installer netboot archive: {exc}") from exc

        preseed_files = self._resolve_preseed_files()
        if preseed_files:
            shutil.copy2(preseed_files[0], staging / "preseed.cfg")
        installer_files = resolve_from_project("configs/debian-installer")
        generated = [*autologin_entries(self.config, self.runtime), *locale_entries(self.config, self.runtime), *grub_entries(self.config, self.runtime)]
        from .installer_source import default_preseed
        preseed = default_preseed(preseed_files[0].read_text() if preseed_files else "", self.config, self.runtime["installer"])
        for name in ("early_command.sh", "late_command.sh"):
            source = installer_files / name
            if source.is_file():
                shutil.copy2(source, staging / name)
                kind = name.split("_", 1)[0]
                path = self.runtime["installer"]["customization"][kind + "_script_path"]
                generated.append(Entry(path, source.read_bytes(), 0o100755))
                preseed += f"\nd-i preseed/{kind}_command string /bin/sh /{path}\n"
        if preseed.strip():
            if self.config.get("preseed") or self.runtime["installer"]["customization"]["embed_preseed"]:
                generated.append(Entry("preseed.cfg", preseed.encode(), 0o100644))
            (staging / "preseed.cfg").write_text(preseed)
        customization = self.runtime["installer"]["customization"]
        if generated or customization["enabled"]:
            for path in sorted(staging.rglob("initrd.gz")):
                if path.is_symlink():
                    continue
                frontend = "gtk" if "gtk" in path.relative_to(staging).parts else "text"
                customize_initrd(path, customization, frontend, generated=generated,
                                 epoch=self.runtime["build"]["reproducibility"]["source_date_epoch"])
        self._write_tar_archive(staging, output)
        if not output.is_file() or output.stat().st_size == 0:
            raise ISOEngineError(f"Netboot archive was not created: {output}")
        return output

    def _find_kernel_and_initramfs(self) -> Tuple[str, str]:
        boot_dir = self.target_root / "boot"
        if not boot_dir.exists():
            return "vmlinuz", "initrd.img"

        kernels = {f.name.removeprefix("vmlinuz-"): f.name for f in boot_dir.glob("vmlinuz-*")
                   if f.is_file() and not f.name.endswith((".old", ".bak"))}
        initrds = {f.name.removeprefix("initrd.img-"): f.name for f in boot_dir.glob("initrd.img-*")
                   if f.is_file() and not f.name.endswith((".old", ".bak"))}
        common = set(kernels) & set(initrds)
        if not common:
            if self.mode == "mock":
                return "vmlinuz", "initrd.img"
            raise ISOEngineError("No kernel/initramfs pair with a matching version was found")

        def newer(left: str, right: str) -> bool:
            # Debian's dpkg comparator handles epochs, revisions and tildes.
            try:
                result = subprocess.run(
                    ["dpkg", "--compare-versions", left, "gt", right],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                )
                return result.returncode == 0
            except OSError:
                # Portable fallback for environments without dpkg.
                return [int(part) if part.isdigit() else part for part in re.split(r"([0-9]+)", left)] > [int(part) if part.isdigit() else part for part in re.split(r"([0-9]+)", right)]

        selected = next(iter(common))
        for version in common:
            if newer(version, selected):
                selected = version
        return kernels[selected], initrds[selected]

    def _create_squashfs(self, source_dir: Path, output_path: Path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            output_path.touch()
            return

        if output_path.exists():
            output_path.unlink()

        # Ensure essential mountpoint directories exist in rootfs before creating squashfs
        policy = self.runtime["artifacts"]["squashfs"]
        for d in policy["directories"]:
            (source_dir / d).mkdir(parents=True, exist_ok=True)

        compression = self.config.get("compression", policy["compression"])
        num_cpus = policy["processors"] or os.cpu_count() or policy["fallback_cpus"]
        extra = []
        if policy["sort_file"]:
            sort_file = resolve_from_project(policy["sort_file"])
            if not sort_file.is_file():
                raise ISOEngineError(f"SquashFS sort file not found: {sort_file}")
            extra.extend(["-sort", str(sort_file)])
        if policy["compression_level"] is not None:
            extra.extend(["-Xcompression-level", str(policy["compression_level"])])
        epoch = self.runtime["build"]["reproducibility"]["source_date_epoch"]
        if epoch is not None:
            extra.extend(["-mkfs-time", str(epoch), "-all-time", str(epoch)])
        if not os.isatty(1):
            extra.append("-no-progress")
        logger.info(f"Creating SquashFS with {compression} compression using {num_cpus} cores...")
        self.toolchain.run_tool(
            "mksquashfs",
            [
                str(source_dir),
                str(output_path),
                "-comp", compression,
                "-b", policy["block_size"],
                "-processors", str(num_cpus),
                "-noappend",
                *extra,
                "-wildcards",
                "-e", *policy["exclude"]
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
            self._build_host_root(),
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

    def _stage_signed_loaders(self, roots, destination, targets):
        policy = self.runtime["artifacts"]["iso"]
        if policy["secure_boot"] == "disable":
            return
        primary = self.arch_policy["efi_target"]
        for platform, filename in targets:
            spec = policy["signed_loaders"].get(platform)
            found = False
            if spec:
                for root in roots:
                    shim, grub = root / spec["shim"], root / spec["grub"]
                    if shim.is_file() and grub.is_file():
                        shutil.copy2(shim, destination / filename)
                        shutil.copy2(grub, destination / spec["grub_filename"])
                        mok = root / spec["mok"]
                        if mok.is_file():
                            shutil.copy2(mok, destination / spec["mok_filename"])
                        found = True
                        break
            if not found and platform == primary:
                if policy["secure_boot"] == "require":
                    raise ISOEngineError(f"Secure Boot requires signed Shim and GRUB for {platform}")
                logger.warning("Signed Shim/GRUB unavailable for %s; Secure Boot is not guaranteed", platform)

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
            self._build_host_root(),
            Path("/")
        ]
        primary_platform, primary_filename = self.arch_policy["efi_target"], self.arch_policy["efi_filename"]
        efi_targets = [(primary_platform, primary_filename)]
        for arch in self.arch_policy["compatibility_efi_architectures"]:
            spec = self.runtime["architectures"][arch]
            efi_targets.append((spec["efi_target"], spec["efi_filename"]))

        for efi_platform, _ in efi_targets:
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

        for fmt, boot_filename in efi_targets:
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

        self._stage_signed_loaders(search_roots, iso_efi_dir, efi_targets)

        efi_files = [f for f in iso_efi_dir.glob("*") if f.is_file()]
        if efi_files:
            total_size_mb = max(self.runtime["artifacts"]["iso"]["efi_min_mib"], sum(f.stat().st_size for f in efi_files) // (1024 * 1024) + self.runtime["artifacts"]["iso"]["efi_slack_mib"])
            size_kb = total_size_mb * 1024

            if efiboot_img.exists():
                efiboot_img.unlink()

            epoch = self.runtime["build"]["reproducibility"]["source_date_epoch"]
            fat_options = [] if epoch is None else ["-i", f"{epoch:08x}", "--invariant"]
            if epoch is not None:
                for item in efi_files:
                    os.utime(item, (epoch, epoch))
            mkfs_res = self.toolchain.run_tool("mkfs.vfat", [*fat_options, "-C", str(efiboot_img), str(size_kb)], check=False)
            if mkfs_res.returncode != 0:
                self.toolchain.run_tool("truncate", ["-s", f"{total_size_mb}M", str(efiboot_img)], check=True)
                self.toolchain.run_tool("mformat", ["-i", str(efiboot_img), "-h", "32", "-t", "32", "-n", "64", "-c", "1", "::"], check=False)

            self.toolchain.run_tool("mmd", ["-i", str(efiboot_img), "::/EFI"], check=False)
            self.toolchain.run_tool("mmd", ["-i", str(efiboot_img), "::/EFI/BOOT"], check=False)
            for f in efi_files:
                result = self.toolchain.run_tool("mcopy", ["-m", "-i", str(efiboot_img), str(f), f"::/EFI/BOOT/{f.name}"], check=False)
                if result.returncode != 0:
                    raise ISOEngineError(f"Could not copy EFI file into ESP: {f.name}")

            # The primary architecture executable is mandatory. IA32 is an
            # optional compatibility loader on amd64 media.
            required_efi = {primary_filename}
            missing = [filename for filename in required_efi if not any(f.name == filename for f in efi_files)]
            if missing:
                raise ISOEngineError(f"Missing EFI boot executable(s): {', '.join(sorted(missing))}")

    def _copy_syslinux_binaries(self):
        syslinux_paths = [
            self.target_root / "usr" / "lib" / "ISOLINUX",
            self.target_root / "usr" / "lib" / "syslinux" / "modules" / "bios",
            self.target_root / "usr" / "share" / "syslinux",
            self.target_root / "usr" / "lib" / "syslinux" / "bios",
            self.target_root / "usr" / "lib" / "syslinux",
            self._build_host_root() / "usr" / "lib" / "ISOLINUX",
            self._build_host_root() / "usr" / "lib" / "syslinux" / "modules" / "bios",
            self._build_host_root() / "usr" / "share" / "syslinux",
            self._build_host_root() / "usr" / "lib" / "syslinux" / "bios",
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
            isolinux_template = resolve_from_project("configs/boot/templates/isolinux.cfg.in")
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
                    f"  APPEND initrd=/live/initrd.img {kernel_params} nomodeset\n"
                )
            if self.config.get("di_mode") == "netinstall":
                syslinux_cfg = "UI vesamenu.c32\nPROMPT 0\nTIMEOUT 50\n"
            if self.config.get("with_debian_installer"):
                installer_label = self.runtime["installer"]["menu_labels"][self.config.get("di_mode", "live")]
                syslinux_cfg += (
                    "\nLABEL install\n"
                    f"  MENU LABEL ^{installer_label} (text)\n"
                    "  KERNEL /install/vmlinuz\n"
                    "  APPEND initrd=/install/initrd.gz vga=788 --- quiet\n"
                )
                if self.config.get("di_graphical"):
                    syslinux_cfg += (
                    "\n"
                    "LABEL install-gtk\n"
                    f"  MENU LABEL ^{installer_label} (graphical)\n"
                    "  KERNEL /install/gtk/vmlinuz\n"
                    "  APPEND initrd=/install/gtk/initrd.gz video=vesa:ywrap,mtrr vga=788 --- quiet\n"
                    )
                syslinux_cfg += (
                    "\n"
                    "LABEL install-auto\n"
                    f"  MENU LABEL ^{installer_label} (automated)\n"
                    "  KERNEL /install/vmlinuz\n"
                    "  APPEND initrd=/install/initrd.gz auto=true priority=critical preseed/file=/cdrom/install/preseed.cfg --- quiet\n"
                )

            if self.config.get("with_debian_installer"):
                for entry, kernel, initrd, parameters in self._installer_extra_boot_entries():
                    syslinux_cfg += (
                        f"\nLABEL install-{entry['id']}\n"
                        f"  MENU LABEL {entry['label']}\n"
                        f"  KERNEL {kernel}\n"
                        f"  APPEND initrd={initrd} {parameters} --- quiet\n"
                    )
            (isolinux_target / "isolinux.cfg").write_text(syslinux_cfg)
        elif isolinux_target.exists():
            shutil.rmtree(isolinux_target, ignore_errors=True)

    def _installer_extra_boot_entries(self):
        """Share native d-i entry parameters between GRUB and ISOLINUX."""
        for entry in self.runtime["installer"]["extra_boot_entries"]:
            graphical = entry["frontend"] == "gtk"
            if graphical and not self.config.get("di_graphical"):
                continue
            directory = "/install/gtk" if graphical else "/install"
            parameters = " ".join(["vga=788", *entry["parameters"]])
            yield entry, directory + "/vmlinuz", directory + "/initrd.gz", parameters

    def _write_media_checksums(self):
        policy = self.runtime["artifacts"]["iso"]
        paths = sorted(self.iso_staging.rglob("*"))
        contents = self.iso_staging / ".disk/contents"
        contents.parent.mkdir(parents=True, exist_ok=True)
        names = {path.relative_to(self.iso_staging).as_posix() for path in paths}
        names.add(".disk/contents")
        names.update(algorithm + "sum.txt" for algorithm in policy["checksum_algorithms"])
        contents.write_text("\n".join(sorted(names)) + "\n")
        paths = sorted(self.iso_staging.rglob("*"))
        for algorithm in policy["checksum_algorithms"]:
            records = []
            for path in paths:
                relative = path.relative_to(self.iso_staging).as_posix()
                if path.is_symlink() or not path.is_file() or any(fnmatch.fnmatchcase(relative, pattern) for pattern in policy["checksum_excludes"]):
                    continue
                display = "./" + relative
                escaped = "\\" in display or "\n" in display
                if escaped:
                    display = display.replace("\\", "\\\\").replace("\n", "\\n")
                records.append(("\\" if escaped else "") + f"{file_digest(path, algorithm)}  {display}\n")
            (self.iso_staging / (algorithm + "sum.txt")).write_text("".join(records))

    def _apply_binary_overlays(self):
        root = self.iso_staging.resolve()
        for overlay in self.runtime["artifacts"]["iso"]["binary_overlays"]:
            source = resolve_from_project(overlay["source"])
            if not source.is_dir():
                raise ISOEngineError(f"Binary overlay not found: {source}")
            if source.resolve().is_relative_to(root) or root.is_relative_to(source.resolve()):
                raise ISOEngineError("Binary overlay source overlaps ISO staging")
            destination = self.iso_staging / archive_path(overlay["destination"])
            if not destination.resolve().is_relative_to(root):
                raise ISOEngineError("Binary overlay destination escapes staging")
            destination.mkdir(parents=True, exist_ok=True)
            for directory, dirs, files in os.walk(source, followlinks=False):
                for name in [*dirs, *files]:
                    item = Path(directory) / name
                    target = destination / item.relative_to(source)
                    if not target.parent.resolve().is_relative_to(root):
                        raise ISOEngineError(f"Binary overlay descends through an external link: {target}")
                    if target.is_symlink():
                        target.unlink()
                    if item.is_symlink():
                        if target.exists():
                            if target.is_dir():
                                raise ISOEngineError(f"Binary overlay link conflicts with a directory: {target}")
                            target.unlink()
                        target.symlink_to(os.readlink(item))
                    elif item.is_dir():
                        target.mkdir(exist_ok=True)
                    elif item.is_file():
                        if target.is_dir():
                            raise ISOEngineError(f"Binary overlay file conflicts with a directory: {target}")
                        shutil.copy2(item, target)
                    else:
                        raise ISOEngineError(f"Unsupported binary overlay file: {item}")

    def build_iso(self) -> Path:
        self.prepare_installer_bundle()
        installer_only = self.config.get("di_mode") == "netinstall"
        if self.iso_staging.exists():
            shutil.rmtree(self.iso_staging)
        self.iso_staging.mkdir(parents=True, exist_ok=True)
        if not installer_only:
            (self.iso_staging / "live").mkdir(parents=True, exist_ok=True)
        (self.iso_staging / "isolinux").mkdir(parents=True, exist_ok=True)
        (self.iso_staging / "boot" / "grub").mkdir(parents=True, exist_ok=True)

        if not installer_only:
            kernel, initramfs = self._find_kernel_and_initramfs()
            if self.mode != "mock":
                src_kernel = self.target_root / "boot" / kernel
                src_initramfs = self.target_root / "boot" / initramfs
                if src_kernel.exists():
                    shutil.copy2(src_kernel, self.iso_staging / "live" / "vmlinuz")
                if src_initramfs.exists():
                    shutil.copy2(src_initramfs, self.iso_staging / "live" / "initrd.img")
                if not (self.iso_staging / "live" / "vmlinuz").is_file():
                    raise ISOEngineError(f"Kernel not found below {self.target_root / 'boot'}")
                if not (self.iso_staging / "live" / "initrd.img").is_file():
                    raise ISOEngineError(f"Initramfs not found below {self.target_root / 'boot'}")

            squashfs_path = self.iso_staging / "live" / "filesystem.squashfs"
            if self.mode != "mock" and self.config.get('with_debian_installer') and self.config.get('di_mode', 'live') == 'live':
                from .installer_lifecycle import materialize_boot_hardlinks
                separated = materialize_boot_hardlinks(self.target_root)
                if separated:
                    logger.info('Materialized %s boot hardlinks for separate target filesystems', separated)
            self._create_squashfs(self.target_root, squashfs_path)

        iso_label = self._get_iso_label()
        kernel_params = self._get_kernel_params()
        placeholders = self._get_template_placeholders()

        # 1. Load config.cfg from template if available
        config_template = resolve_from_project("configs/boot/templates/config.cfg.in")
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
        grub_template = resolve_from_project("configs/boot/templates/grub.cfg.in")
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
                f"    linux /live/vmlinuz {kernel_params} nomodeset\n"
                "    initrd /live/initrd.img\n"
                "}\n"
            )
        # Chroot overlays do not automatically become files in the El Torito
        # filesystem. Stage and activate the bundled graphical GRUB theme.
        theme = self.runtime["customization"]["grub"]
        grub_theme_src = resolve_from_project(theme["iso_source"])
        if grub_theme_src.is_dir():
            grub_theme_dst = self.iso_staging / Path(theme["theme_file"]).parent
            shutil.copytree(grub_theme_src, grub_theme_dst, dirs_exist_ok=True)
            grub_cfg_text = theme["iso_activation"].format(theme_file=theme["theme_file"]) + grub_cfg_text

        if installer_only:
            grub_cfg_text = "source /boot/grub/config.cfg\n"

        # 3. Load loopback.cfg from template if available
        loopback_template = resolve_from_project("configs/boot/templates/loopback.cfg.in")
        if loopback_template.exists():
            loopback_cfg_text = loopback_template.read_text()
            for k, v in placeholders.items():
                loopback_cfg_text = loopback_cfg_text.replace(k, str(v))
        else:
            loopback_cfg_text = "source /boot/grub/grub.cfg\n"

        if self.config.get("with_debian_installer"):
            install_dir = self.iso_staging / "install"
            install_gtk_dir = install_dir / "gtk"
            self._stage_debian_installer(install_dir)
            live_installer = self.config.get("di_mode", "live") == "live"
            if live_installer and self.mode != "mock":
                try:
                    LiveInstallerMedia(self.config, self.runtime["installer"]).prepare(self.iso_staging, self.toolchain)
                except (OSError, ValueError, urllib.error.URLError) as exc:
                    raise ISOEngineError(f"Could not prepare live Debian Installer media: {exc}") from exc

            di_custom_dir = resolve_from_project("configs/debian-installer")
            preseed_files = self._resolve_preseed_files()

            preseed_blocks = []
            for pf in preseed_files:
                try:
                    from .installer_source import default_preseed
                    preseed_blocks.append(f"# --- Preseed: {pf.name} ---\n" + default_preseed(pf.read_text(), self.config, self.runtime["installer"]))
                except Exception as e:
                    logger.warning(f"Could not read preseed file {pf}: {e}")

            has_preseed = len(preseed_blocks) > 0
            from .installer_source import default_preseed
            preseed_content = "\n".join(preseed_blocks) if has_preseed else default_preseed("", self.config, self.runtime["installer"])
            generated = [] if live_installer else [*autologin_entries(self.config, self.runtime), *locale_entries(self.config, self.runtime), *grub_entries(self.config, self.runtime)]

            if (di_custom_dir / "early_command.sh").exists():
                shutil.copy2(di_custom_dir / "early_command.sh", install_dir / "early_command.sh")
                try:
                    (install_dir / "early_command.sh").chmod(0o755)
                except Exception:
                    pass
                early_path = archive_path(self.runtime["installer"]["customization"]["early_script_path"])
                generated.append(Entry(early_path, (di_custom_dir / "early_command.sh").read_bytes(), 0o100755))
                preseed_content += f"\nd-i preseed/early_command string /bin/sh /{early_path}\n"

            if not live_installer and (di_custom_dir / "late_command.sh").exists():
                shutil.copy2(di_custom_dir / "late_command.sh", install_dir / "late_command.sh")
                try:
                    (install_dir / "late_command.sh").chmod(0o755)
                except Exception:
                    pass
                late_path = archive_path(self.runtime["installer"]["customization"]["late_script_path"])
                generated.append(Entry(late_path, (di_custom_dir / "late_command.sh").read_bytes(), 0o100755))
                preseed_content += f"\nd-i preseed/late_command string /bin/sh /{late_path}\n"

            if (di_custom_dir / "splash.png").exists():
                shutil.copy2(di_custom_dir / "splash.png", install_gtk_dir / "splash.png")
                preseed_content += "\nd-i debian-installer/splashpng string /cdrom/install/gtk/splash.png\n"

            if preseed_content.strip():
                (install_dir / "preseed.cfg").write_text(preseed_content)

            if live_installer:
                media = LiveInstallerMedia(self.config, self.runtime["installer"])
                preseed_content += "\n" + "\n".join(self.runtime["installer"]["live_preseed"]) + "\n"
                (install_dir / "preseed.cfg").write_text(preseed_content)
                if self.mode != "mock":
                    media.embed_preseed(install_dir, preseed_content, generated_extra=generated, source_root=self.target_root)
            elif self.mode != "mock":
                if preseed_content.strip() and (self.config.get("preseed") or self.runtime["installer"]["customization"]["embed_preseed"]):
                    generated.append(Entry("preseed.cfg", preseed_content.encode(), 0o100644))
                customization = self.runtime["installer"]["customization"]
                if generated or customization["enabled"]:
                    for frontend, path in (("text", install_dir / "initrd.gz"), ("gtk", install_gtk_dir / "initrd.gz")):
                        if path.is_file():
                            customize_initrd(path, customization, frontend, generated=generated,
                                             epoch=self.runtime["build"]["reproducibility"]["source_date_epoch"])

            installer_label = self.runtime["installer"]["menu_labels"][self.config.get("di_mode", "live")]

            grub_cfg_text += (
                f"\nmenuentry '{installer_label} (text)' {{\n"
                "    linux /install/vmlinuz vga=788 --- quiet\n"
                "    initrd /install/initrd.gz\n"
                "}\n"
            )
            if self.config.get("di_graphical"):
                grub_cfg_text += (
                "\n"
                f"menuentry '{installer_label} (graphical)' {{\n"
                "    linux /install/gtk/vmlinuz video=vesa:ywrap,mtrr vga=788 --- quiet\n"
                "    initrd /install/gtk/initrd.gz\n"
                "}\n"
                )

            if (install_dir / "preseed.cfg").exists():
                grub_cfg_text += (
                    f"\nmenuentry '{installer_label} (automated)' {{\n"
                    "    linux /install/vmlinuz auto=true priority=critical preseed/file=/cdrom/install/preseed.cfg --- quiet\n"
                    "    initrd /install/initrd.gz\n"
                    "}\n"
                )

            for entry, kernel, initrd, parameters in self._installer_extra_boot_entries():
                grub_cfg_text += (
                    f"\nmenuentry '{entry['label']}' {{\n"
                    f"    linux {kernel} {parameters} --- quiet\n"
                    f"    initrd {initrd}\n"
                    "}\n"
                )

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
            self._build_host_root() / "usr" / "share" / "grub" / "unicode.pf2",
            Path("/usr/share/grub/unicode.pf2"),
        ]:
            if font_candidate.exists():
                shutil.copy2(font_candidate, self.iso_staging / "boot" / "grub" / "unicode.pf2")
                break

        platforms = [self.arch_policy["efi_target"], *[self.runtime["architectures"][arch]["efi_target"] for arch in self.arch_policy["compatibility_efi_architectures"]]]
        for platform in platforms:
            platform_dir = self.iso_staging / "boot" / "grub" / platform
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
        if self.mode != "mock":
            if self.should_use_syslinux() and not (self.iso_staging / "isolinux" / "isolinux.bin").is_file():
                raise ISOEngineError("Syslinux was selected but isolinux.bin was not found")
            if self.should_use_grub_bios() and (not bios_core.is_file() or bios_core.stat().st_size == 0):
                raise ISOEngineError("GRUB BIOS was selected but its El Torito image could not be built")
            efi_image = self.iso_staging / "boot" / "grub" / "efiboot.img"
            if self.should_use_grub_efi() and (not efi_image.is_file() or efi_image.stat().st_size == 0):
                raise ISOEngineError("GRUB UEFI was selected but its EFI system image could not be built")

        # Package metadata comes from installed dpkg paragraphs, including the
        # final paragraph even when status does not end with a blank line.
        offline_repo_dir = self.config.get("offline_repo_dir")
        if self.mode != "mock" and offline_repo_dir and Path(offline_repo_dir).is_dir():
            shutil.copytree(offline_repo_dir, self.iso_staging / "repo", dirs_exist_ok=True,
                            symlinks=True, ignore_dangling_symlinks=True)
        self._apply_binary_overlays()
        inventory = installed_packages(self.target_root)
        if not installer_only:
            manifest = "".join(f"{entry['Package']}\t{entry['Version']}\n" for _, entry in sorted(inventory.items()))
            (self.iso_staging / "live/filesystem.packages").write_text(manifest)
        self._write_media_checksums()

        iso_path = self._resolve_output_path("iso")
        iso_path.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "mock":
            iso_path.touch()
        else:
            search_roots = [
                self.target_root,
                self._build_host_root(),
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

            self._publish_iso(iso_path, xorriso_args)

        return iso_path

    def build_tarball(self) -> Path:
        tar_path = resolve_output_path(self.output_name, ".tar.xz")
        tar_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "mock":
            tar_path.touch()
        else:
            epoch = self.runtime["build"]["reproducibility"]["source_date_epoch"]
            options = [] if epoch is None else ["--sort=name", f"--mtime=@{epoch}", "--numeric-owner"]
            with tempfile.TemporaryDirectory(prefix=".rootfs-tar-", dir=tar_path.parent) as directory:
                pending = Path(directory) / tar_path.name
                self.toolchain.run_tool("tar", [
                    "cJpf", str(pending), *options,
                    "--exclude=./proc/*", "--exclude=./sys/*", "--exclude=./dev/*",
                    "--exclude=./tmp/*", "--exclude=./run/*",
                    "-C", str(self.target_root), "."
                ], check=True)
                if not pending.is_file() or not pending.stat().st_size:
                    raise ISOEngineError("Rootfs tarball was not created")
                os.replace(pending, tar_path)
            if not tar_path.is_file() or tar_path.stat().st_size == 0:
                raise ISOEngineError(f"Rootfs tarball was not created: {tar_path}")
        return tar_path
