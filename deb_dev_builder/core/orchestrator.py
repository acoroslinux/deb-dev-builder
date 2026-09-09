import os
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Any
from deb_dev_builder.core.chroot_manager import ChrootManager
from deb_dev_builder.core.toolchain_manager import ToolchainManager
from deb_dev_builder.core.apt_manager import APTManager
from deb_dev_builder.core.customizer import SystemCustomizer
from deb_dev_builder.core.iso_engine import ISOEngine
from deb_dev_builder.core.disk_engine import DiskEngine
from deb_dev_builder.core.container_engine import ContainerEngine
from deb_dev_builder.core.config_loader import ConfigLoader
from deb_dev_builder.core.path_utils import resolve_from_project, safe_remove_tree, unmount_all_under, mountpoints_under
from deb_dev_builder.core.chroot_cleaner import ChrootCleaner
from deb_dev_builder.core.hook_manager import HookManager
import logging

logger = logging.getLogger("orchestrator")

_VALID_LOGIN_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,30}\$?$")

class BuildOrchestratorError(Exception):
    pass

class BuildOrchestrator:
    def __init__(
        self,
        arch: str = "amd64",
        config_path: str = "configs/global_build.json",
        distro: Optional[str] = None,
        init_system: Optional[str] = None,
        desktop: Optional[str] = None,
        kernel: Optional[str] = None,
        bootloader: Optional[str] = None,
        fs_type: str = "ext4",
        variant: Optional[str] = None,
        package_profiles: Optional[List[str]] = None,
        service_profiles: Optional[List[str]] = None,
        repo_profiles: Optional[List[str]] = None,
        live_profile: Optional[str] = None,
        output_format: str = "iso",
        mode: str = "mock",
        clean: bool = True,
        fast_mode: bool = False,
        use_tmpfs: bool = False,
        generate_manifest: bool = True,
        with_calamares: bool = False,
        with_debian_installer: bool = False,
        preseed: Optional[str] = None,
        di_mode: str = "live",
        use_seed: bool = True,
        recreate_seed: bool = False,
        multimedia_codecs: bool = False,
        with_flathub: bool = False,
        with_zram: bool = False,
        with_offline_repo: bool = False,
        offline_repo_packages: Optional[List[str]] = None,
        force_isolated_toolchain: bool = False,
        compression: Optional[str] = None,
        hostname: Optional[str] = None,
        live_user_name: Optional[str] = None,
        hooks_dir: Optional[str] = None,
        hooks_enabled: bool = True,
        rootfs_cleanup: bool = True,
        hardware_profile: Optional[str] = None,
        vm_profile: Optional[str] = None,
    ):
        self.arch = arch
        self.config_path = config_path
        self.distro = distro
        self.init_system = init_system or ("sysvinit" if str(distro).startswith("devuan-") else "systemd")
        self.desktop = desktop
        self.kernel = kernel
        self._bootloader_was_explicit = bootloader is not None
        self.bootloader = bootloader
        self.fs_type = fs_type
        self.variant = variant
        self.package_profiles = package_profiles or []
        self.service_profiles = service_profiles or []
        self.repo_profiles = repo_profiles or []
        self.live_profile = live_profile
        self.output_format = output_format.lower()
        self.mode = mode.lower()
        self.clean = clean
        self.fast_mode = fast_mode
        self.use_tmpfs = use_tmpfs
        self.generate_manifest = generate_manifest
        self.with_calamares = with_calamares
        self.with_debian_installer = with_debian_installer
        self.preseed = preseed
        self.di_mode = di_mode.lower()
        self.use_seed = use_seed
        self.recreate_seed = recreate_seed
        self.multimedia_codecs = multimedia_codecs
        self.with_flathub = with_flathub
        self.with_zram = with_zram
        self.with_offline_repo = with_offline_repo
        self.offline_repo_packages = offline_repo_packages or []
        self.force_isolated_toolchain = force_isolated_toolchain
        self.compression = compression
        self.hostname = hostname
        self.live_user_name = live_user_name
        self.hooks_dir = hooks_dir
        self.hooks_enabled = hooks_enabled
        self.rootfs_cleanup = rootfs_cleanup
        self.hardware_profile = hardware_profile
        self.vm_profile = vm_profile

        # A desktop live image should be useful immediately after boot. Keep
        # these defaults scoped to that use case so minimal/server/iot builds
        # remain small and predictable.
        if self.variant == "live" and self.desktop:
            audio_profiles = {"audio", "pipewire", "pulseaudio"}
            for profile in (
                "filesystems", "networking", "network-shares", "printing",
                "security", "system-utils", "multimedia",
            ):
                if profile not in self.package_profiles:
                    self.package_profiles.append(profile)
            if not audio_profiles.intersection(self.package_profiles):
                self.package_profiles.append("pipewire")

        # Graphical builds should provide a reliable native package manager;
        # non-graphical/server images do not need the GTK application.
        if self.desktop and "desktop-utils" not in self.package_profiles:
            self.package_profiles.append("desktop-utils")

        # GTK desktops benefit from a tray update indicator and PackageKit
        # integration. KDE provides its own Discover workflow, so avoid
        # pulling duplicate update frontends there.
        if self.desktop and str(self.desktop).lower() != "kde" and "update-tools" not in self.package_profiles:
            self.package_profiles.append("update-tools")

        # --- SMART BOOTLOADER DEFAULTS ---
        if not self.bootloader:
            if self.output_format == "iso" and self.arch in {"amd64", "x86_64", "i386", "i686"}:
                self.bootloader = {"type": "grub2-hybrid"}
            else:
                self.bootloader = {"type": "grub2-uefi"}
                
        # We don't inject extra packages here because debian uses package profiles
        # But we could ensure grub-efi is installed.
        # ---------------------------------

        if self.multimedia_codecs and "multimedia" not in self.package_profiles:
            self.package_profiles.append("multimedia")
        if self.with_offline_repo and "offline-repo" not in self.package_profiles:
            self.package_profiles.append("offline-repo")
        if self.with_calamares and "calamares" not in self.package_profiles:
            self.package_profiles.append("calamares")
        bootable_formats = {"iso", "img", "raw", "qcow2", "vmdk", "vhd", "vhdx", "vdi"}
        if self.output_format in bootable_formats and self.di_mode != "netinstall" and "firmware" not in self.package_profiles:
            self.package_profiles.append("firmware")

        self.workdir = resolve_from_project(f"workdir/{self.arch}")
        self.target_root = self.workdir / "chroot"
        self.loader = ConfigLoader()

        cfg_file = resolve_from_project(self.config_path)
        self.config = self.loader.assemble_build_config(
            global_config_path=cfg_file,
            architecture=self.arch,
            distro=self.distro,
            init_system=self.init_system,
            desktop=self.desktop,
            kernel=self.kernel,
            bootloader=self.bootloader,
            variant=self.variant,
            package_profiles=self.package_profiles,
            service_profiles=self.service_profiles,
            repo_profiles=self.repo_profiles,
            live_profile=self.live_profile,
            hardware_profile=self.hardware_profile,
            vm_profile=self.vm_profile,
        )
        if self.hardware_profile and not self._bootloader_was_explicit:
            hardware_bootloader = self.config.get("hardware", {}).get("bootloader")
            if hardware_bootloader:
                self.bootloader = hardware_bootloader
        configured_bootloader = self.config.get("bootloader", {})
        configured_type = configured_bootloader.get("type") if isinstance(configured_bootloader, dict) else configured_bootloader
        selected_bootloader = self.bootloader or configured_type or "grub2-hybrid"
        if isinstance(selected_bootloader, dict):
            selected_bootloader = selected_bootloader.get("type", "grub2-hybrid")
        bootloader_config = dict(configured_bootloader) if isinstance(configured_bootloader, dict) else {}
        bootloader_config["type"] = selected_bootloader
        self.config["bootloader"] = bootloader_config
        self.config["bootloader_type"] = selected_bootloader
        self.config["fs_type"] = self.fs_type
        self.config["with_calamares"] = self.with_calamares
        self.config["with_debian_installer"] = self.with_debian_installer
        self.config["preseed"] = self.preseed
        self.config["di_mode"] = self.di_mode
        self.config["with_flathub"] = self.with_flathub
        self.config["with_zram"] = self.with_zram
        self.config["output_format"] = self.output_format
        if self.compression:
            self.config["compression"] = self.compression
        if self.hostname:
            self.config["hostname"] = self.hostname
        if self.live_user_name:
            live_user_config = self.config.get("live_user", {})
            if not isinstance(live_user_config, dict):
                live_user_config = {}
            live_user_config["name"] = self.live_user_name
            self.config["live_user"] = live_user_config
        if self.with_flathub and "flatpak" not in self.config.get("software", []):
            self.config.setdefault("software", []).append("flatpak")
        if str(self.distro).startswith("devuan-") and "devuan-keyring" not in self.config.get("software", []):
            self.config.setdefault("software", []).append("devuan-keyring")

        init_package = "live-config-systemd" if self.init_system == "systemd" else "live-config-sysvinit"
        if self.output_format == "iso":
            essential_boot_pkgs = ["dosfstools", "mtools"]
            if self.di_mode != "netinstall":
                essential_boot_pkgs += ["live-boot", "live-config", init_package]
            if self.arch in {"amd64", "x86_64"}:
                essential_boot_pkgs += ["grub-pc-bin", "grub-efi-amd64-bin", "grub-efi-ia32-bin", "isolinux", "syslinux-common"]
                if str(self.distro).startswith("debian-"):
                    essential_boot_pkgs.append("shim-signed")
            elif self.arch in {"i386", "i686"}:
                essential_boot_pkgs += ["grub-pc-bin", "grub-efi-ia32-bin", "isolinux", "syslinux-common"]
            elif self.arch in {"aarch64", "arm64"}:
                essential_boot_pkgs.append("grub-efi-arm64-bin")
            elif self.arch == "armhf":
                essential_boot_pkgs.append("grub-efi-arm-bin")
            elif self.arch == "riscv64":
                essential_boot_pkgs.append("grub-efi-riscv64-bin")
        elif self.output_format in {"img", "raw", "qcow2", "vmdk", "vhd", "vhdx", "vdi"}:
            essential_boot_pkgs = ["dosfstools", "mtools", "initramfs-tools"]
            if self.init_system == "systemd":
                essential_boot_pkgs.append("systemd-sysv")
            if selected_bootloader == "systemd-boot":
                essential_boot_pkgs.append("systemd-boot")
            elif "grub" in selected_bootloader:
                grub_packages = {
                    "amd64": "grub-efi-amd64-bin", "x86_64": "grub-efi-amd64-bin",
                    "i386": "grub-efi-ia32-bin", "i686": "grub-efi-ia32-bin",
                    "aarch64": "grub-efi-arm64-bin", "arm64": "grub-efi-arm64-bin",
                    "armhf": "grub-efi-arm-bin", "riscv64": "grub-efi-riscv64-bin",
                }
                if self.arch in grub_packages:
                    essential_boot_pkgs.append(grub_packages[self.arch])
            if self.fs_type == "btrfs":
                essential_boot_pkgs.append("btrfs-progs")
            elif self.fs_type == "f2fs":
                essential_boot_pkgs.append("f2fs-tools")
            elif self.fs_type == "xfs":
                essential_boot_pkgs.append("xfsprogs")
        else:
            essential_boot_pkgs = []
        for pkg in essential_boot_pkgs:
            if pkg not in self.config.get("software", []):
                self.config.setdefault("software", []).append(pkg)

        if self.arch not in {"amd64", "x86_64", "i386", "i686"}:
            unsupported_x86_boot = ("grub-pc", "grub-efi-amd64", "grub-efi-ia32", "isolinux", "syslinux", "shim-signed")
            self.config["software"] = [
                package for package in self.config.get("software", [])
                if not package.startswith(unsupported_x86_boot)
            ]
        elif self.arch in {"i386", "i686"}:
            self.config["software"] = [
                package for package in self.config.get("software", [])
                if not package.startswith(("grub-efi-amd64", "shim-signed"))
            ]

    def validate(self) -> Dict[str, Any]:
        errors = []
        if not self.distro:
            errors.append("Distro profile not specified.")
        if self.output_format not in {"iso", "netboot", "img", "raw", "qcow2", "vmdk", "vdi", "vhd", "vhdx", "tarball", "container", "oci"}:
            errors.append(f"Unsupported output format: {self.output_format}")
        if str(self.distro).startswith("devuan-") and self.init_system == "systemd":
            errors.append("Devuan builds cannot use systemd; choose sysvinit, openrc, or runit.")
        supported_suites = self.config.get("supported_suites")
        if supported_suites and self.config.get("suite") not in supported_suites:
            errors.append(
                f"Profile selection is not available for suite {self.config.get('suite')}; "
                f"supported suites: {', '.join(supported_suites)}."
            )
        if self.di_mode in {"netinstall", "netboot"} and not self.with_debian_installer:
            errors.append(f"Debian Installer mode '{self.di_mode}' requires --with-debian-installer.")
        if self.with_debian_installer and self.config.get("base_distro") != "debian":
            errors.append("Official Debian Installer media can only be built from Debian profiles.")
        if self.di_mode == "netinstall" and self.output_format != "iso":
            errors.append("Debian Installer netinstall mode requires ISO output.")
        if self.di_mode == "netboot" and self.output_format != "netboot":
            errors.append("Debian Installer netboot mode requires netboot output.")
        if self.output_format == "netboot" and self.di_mode != "netboot":
            errors.append("Netboot output requires --di-mode netboot.")
        if self.with_calamares and self.output_format != "iso":
            errors.append("Calamares is only supported on live ISO output.")
        if self.with_calamares and not self.desktop:
            errors.append("Calamares requires a desktop profile.")
        if self.with_calamares and self.di_mode != "live":
            errors.append("Calamares cannot be combined with installer-only netinstall/netboot modes.")
        if self.preseed:
            requested = Path(self.preseed)
            preseed_dir = resolve_from_project("configs/debian-installer")
            preseed_candidates = (
                requested,
                preseed_dir / f"{self.preseed}.cfg",
                preseed_dir / f"preseed-{self.preseed}.cfg",
                preseed_dir / self.preseed,
            )
            if not any(candidate.is_file() for candidate in preseed_candidates):
                errors.append(f"Preseed configuration not found: {self.preseed}")
        if self.hardware_profile and self.vm_profile:
            errors.append("Hardware device and VM profiles cannot be combined.")
        if self.vm_profile:
            expected_format = self.config.get("vm", {}).get("output_format")
            if expected_format and self.output_format != expected_format:
                errors.append(f"VM profile '{self.vm_profile}' requires {expected_format} output.")
        live_user = self.config.get("live_user", {})
        live_user_name = live_user.get("name") if isinstance(live_user, dict) else live_user
        if not isinstance(live_user_name, str) or not _VALID_LOGIN_NAME.fullmatch(live_user_name):
            errors.append("Live user name must be a valid Linux login name.")
        if isinstance(live_user, dict) and "password" in live_user:
            password = live_user["password"]
            if not isinstance(password, str) or "\n" in password or "\r" in password:
                errors.append("Live user password must be a single-line string.")
        if not self.config.get("software"):
            errors.append("No packages were loaded from the selected profiles.")
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "summary": {
                "arch": self.arch,
                "distro": self.distro,
                "desktop": self.desktop or "(none)",
                "variant": self.variant or "live",
            }
        }

    def build(self, output_name: Optional[str] = None) -> Path:
        if self.mode == "mock":
            return self._build(output_name)
        # Every architecture shares build_host and the cleanup scope. Lock
        # outside workdir before any process can unmount or remove that tree.
        import fcntl
        lock_path = resolve_from_project("cache/build.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BuildOrchestratorError(
                    "Another real build is using this project; wait for it to finish before rebuilding."
                ) from exc
            try:
                return self._build(output_name)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _build(self, output_name: Optional[str] = None) -> Path:
        validation = self.validate()
        if not validation["valid"]:
            raise BuildOrchestratorError("Invalid build configuration: " + "; ".join(validation["errors"]))
        name = output_name or f"deb-dev-{self.distro}-{self.arch}"

        requested_output = Path(name).expanduser()
        if requested_output.is_absolute() or requested_output.parent != Path("."):
            requested_resolved = requested_output.resolve()
            workdir_resolved = self.workdir.resolve()
            if requested_resolved == workdir_resolved or workdir_resolved in requested_resolved.parents:
                raise BuildOrchestratorError(
                    f"Output path must not be inside the disposable workdir: {requested_resolved}"
                )

        if self.clean and self.mode != "mock":
            if os.geteuid() == 0:
                unmount_all_under(resolve_from_project("workdir"))
            if self.workdir.exists():
                safe_remove_tree(self.workdir, allowed_root=resolve_from_project("workdir"))

        if getattr(self, "use_tmpfs", False):
            if getattr(self, "mode", "real") == "real" and __import__("os").geteuid() == 0:
                tmpfs_size = "16G"
                try:
                    total_kb = 0
                    with open("/proc/meminfo", "r") as f:
                        for line in f:
                            if line.startswith("MemTotal:") or line.startswith("SwapTotal:"):
                                total_kb += int(line.split()[1])
                    total_gb = total_kb / (1024 * 1024)
                    safe_gb = max(4, min(16, int(total_gb * 0.75)))
                    tmpfs_size = f"{safe_gb}G"
                except Exception:
                    pass

                try:
                    resolved_workdir = str(self.workdir.resolve())
                    with open("/proc/mounts", "r") as f:
                        if any(len(line.split()) >= 2 and line.split()[1] == resolved_workdir for line in f):
                            import subprocess
                            subprocess.run(["umount", "-f", resolved_workdir], check=False)
                except Exception:
                    pass

                print(f"[ORCHESTRATOR] 🚀 Mounting tmpfs ({tmpfs_size} RAM disk) on {self.workdir}...")
                self.workdir.mkdir(parents=True, exist_ok=True)
                import subprocess
                subprocess.run(["mount", "-t", "tmpfs", "-o", f"size={tmpfs_size},mode=0755", "tmpfs", str(self.workdir)], check=True)
                self._tmpfs_mounted = True
            else:
                print(f"[ORCHESTRATOR] 🚀 [MOCK/SIM] Fast RAM staging enabled for {self.workdir}")

        # Ensure workdir exists before any hooks or toolchain operations
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.target_root.mkdir(parents=True, exist_ok=True)

        if not hasattr(self, "config"):
            self.config = {}
        if hasattr(self.config, "_data"):
            self.config._data["fast_mode"] = self.fast_mode
            self.config._data["use_tmpfs"] = self.use_tmpfs
        elif isinstance(self.config, dict):
            self.config["fast_mode"] = self.fast_mode
            self.config["use_tmpfs"] = self.use_tmpfs

        required_tools = ["tar", "xz"] if self.output_format == "tarball" else ["tar"]
        if self.output_format == "iso":
            required_tools = ["xorriso", "grub-mkstandalone", "mcopy", "mmd", "mkfs.vfat"]
            if self.di_mode != "netinstall":
                required_tools.insert(0, "mksquashfs")
        elif self.output_format == "netboot":
            required_tools = ["tar", "gzip"]
        elif self.output_format in {"img", "raw", "qcow2", "vmdk", "vhd", "vhdx", "vdi"}:
            required_tools = ["truncate", "mke2fs", "mkfs.fat", "mcopy", "parted", "qemu-img"]
            if self.config.get("compression"):
                compressor = {"xz": "xz", "gzip": "gzip", "lz4": "lz4"}.get(self.config["compression"], "zstd")
                required_tools.append(compressor)
            fs_tools = {"btrfs": "mkfs.btrfs", "f2fs": "mkfs.f2fs", "xfs": "mkfs.xfs"}
            if self.fs_type in fs_tools:
                required_tools.append(fs_tools[self.fs_type])
            if self.fs_type == "f2fs":
                required_tools.append("sload.f2fs")

        toolchain = ToolchainManager(
            workdir_base=self.workdir,
            mode=self.mode,
            force_isolated=self.force_isolated_toolchain,
            target_arch=self.arch,
            distro=self.distro,
            required_tools=required_tools,
            toolchain_config=self.config.get("toolchain", {}),
        )
        chroot = ChrootManager(self.target_root, self.mode, cache_dir=resolve_from_project(f"cache/{self.arch}"), arch=self.arch)
        artifact = None
        hooks = HookManager(
            chroot, self.config, hooks_dir=self.hooks_dir,
            workdir=self.workdir, enabled=self.hooks_enabled,
        )
        try:
            hooks.run_stage("pre-chroot")
            toolchain.setup()
            # The isolated toolchain must be mounted before bootstrap so that
            # mmdebstrap/debootstrap never executes from the host.
            toolchain.mount_virtual_fs()
            if self.config.get("base_distro") == "devuan":
                toolchain.install_devuan_keyring()
                toolchain.ensure_devuan_debootstrap_scripts([self.config.get("suite", "ceres")])
            installer_only = self.output_format == "netboot" or self.di_mode == "netinstall"
            if installer_only:
                iso_engine = ISOEngine(self.workdir, self.target_root, name, self.config, self.mode, toolchain)
                artifact = (
                    iso_engine.build_netboot_archive()
                    if self.output_format == "netboot"
                    else iso_engine.build_iso()
                )
                self._verify_artifact(artifact)
                hooks.run_stage("post-chroot", artifact=artifact)
                self._verify_artifact(artifact)
                if self.generate_manifest and artifact.exists():
                    self._generate_checksums(artifact, chroot=chroot)
                self._fix_output_permissions(artifact.parent)
                return artifact

            apt = APTManager(chroot, self.config, toolchain=toolchain)
            suite = self.config.get("suite", "bookworm")
            apt.bootstrap_rootfs(suite, self.config.get("dpkg_arch", self.arch), use_seed=self.use_seed, recreate_seed=self.recreate_seed, reuse_existing=not self.clean)
            toolchain.mount_virtual_fs()
            chroot.mount_virtual_fs()
            apt.configure_sources_list()
            apt.update_apt_cache()

            pkgs = list(self.config.get("software", []))
            zram_package = "systemd-zram-generator" if self.init_system == "systemd" else "zram-tools"
            if self.with_zram and zram_package not in pkgs:
                pkgs.append(zram_package)
            apt.install_packages(pkgs)

            # Prepare offline package repository if requested
            # The catalog profile is opt-in: merely defining package names
            # must not create an offline repository unless requested.
            offline_pkgs = list(self.config.get("offline_repo_packages", [])) if self.with_offline_repo else []
            if self.offline_repo_packages:
                for p in self.offline_repo_packages:
                    if p not in offline_pkgs:
                        offline_pkgs.append(p)
            # Add only stable NVIDIA driver families declared for this exact
            # distribution release and architecture.  Availability is checked
            # against the target APT indexes so Devuan does not inherit Debian
            # packages that it does not ship.
            driver_catalog = resolve_from_project(f"configs/drivers/{self.distro}.json")
            if driver_catalog.is_file():
                try:
                    catalog = json.loads(driver_catalog.read_text(encoding="utf-8"))
                    for family in catalog.get("families", {}).values():
                        supported_arches = family.get("architectures", [])
                        if self.arch not in supported_arches and self.config.get("dpkg_arch") not in supported_arches:
                            continue
                        for package in family.get("packages", []):
                            available = True
                            if self.mode != "mock":
                                probe = chroot.run_in_chroot(
                                    ["apt-cache", "show", package],
                                    check=False, capture_output=True, text=True,
                                )
                                available = probe.returncode == 0 and bool(probe.stdout.strip())
                            if available and package not in offline_pkgs:
                                offline_pkgs.append(package)
                except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
                    logger.warning("Could not load stable driver catalog %s: %s", driver_catalog, exc)
            # Do not duplicate packages already installed in the live root.
            # Keep an optional architecture/version suffix intact while
            # comparing the package name itself.
            installed_names = {
                package.split("=", 1)[0].split(":", 1)[0]
                for package in self.config.get("software", [])
            }
            offline_pkgs = [
                package for package in offline_pkgs
                if package.split("=", 1)[0].split(":", 1)[0] not in installed_names
            ]

            if (self.with_offline_repo or self.offline_repo_packages) and self.output_format == "iso":
                offline_repo_dir = self.workdir / "offline_repo"
                apt.download_offline_packages(offline_pkgs, offline_repo_dir)
                self.config["offline_repo_dir"] = str(offline_repo_dir)
                self.config["with_offline_repo"] = True

            customizer = SystemCustomizer(chroot, self.config)
            customizer.configure_live_environment()
            
            # --- INSTALL BOOTLOADER IN CHROOT (DEBIAN SPECIFIC) ---
            disk_formats = {"img", "raw", "qcow2", "vmdk", "vhd", "vhdx", "vdi"}
            if self.output_format in disk_formats:
                bcfg = self.config.get("bootloader", {})
                btype = bcfg.get("type", "") if isinstance(bcfg, dict) else (bcfg or "")
                
                if self.mode == "real":
                    if "grub" in btype:
                        print(f"\n[ORCHESTRATOR] Installing GRUB Bootloader ({btype}) into chroot /boot/efi...")
                        chroot.run_in_chroot(["mkdir", "-p", "/boot/efi/EFI"])
                        grub_target = {
                            "amd64": "x86_64-efi", "x86_64": "x86_64-efi",
                            "i386": "i386-efi", "i686": "i386-efi",
                            "aarch64": "arm64-efi", "arm64": "arm64-efi",
                            "armhf": "arm-efi", "riscv64": "riscv64-efi",
                        }.get(self.arch, "x86_64-efi")
                        chroot.run_in_chroot(["grub-install", f"--target={grub_target}", "--efi-directory=/boot/efi", "--bootloader-id=debian", "--removable", "--no-nvram"], check=True)
                        chroot.run_in_chroot(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"], check=True)
                    elif "systemd-boot" in btype:
                        print(f"\n[ORCHESTRATOR] Installing systemd-boot Bootloader ({btype}) into chroot /boot/efi...")
                        chroot.run_in_chroot(["mkdir", "-p", "/boot/efi/EFI"])
                        chroot.run_in_chroot(["bootctl", "install", "--esp-path=/boot/efi"], check=False)
                else:
                    print(f"\n[MOCK] Simulated bootloader installation: {btype}")
            # ----------------------------------------------------

            hooks.run_stage("chroot")
            chroot.umount_virtual_fs()
            unmount_all_under(self.target_root)
            # Some kernels retain descendants of recursive /dev binds after
            # individual lazy unmounts.  Perform one explicit tree detach at
            # the exact target root before declaring the chroot dirty.
            if os.geteuid() == 0 and mountpoints_under(self.target_root):
                for virtual_root in ("dev", "sys", "proc"):
                    candidate = self.target_root / virtual_root
                    if candidate.exists():
                        subprocess.run(
                            ["umount", "--recursive", "--lazy", str(candidate)],
                            check=False, stderr=subprocess.DEVNULL,
                        )
                unmount_all_under(self.target_root)
            if os.geteuid() == 0 and mountpoints_under(self.target_root):
                raise BuildOrchestratorError(f"Target root still has mounted host paths: {mountpoints_under(self.target_root)}")
            if self.rootfs_cleanup:
                cleanup_report = ChrootCleaner(self.target_root, self.config).clean()
                logger.info("Rootfs cleanup removed %d entries", cleanup_report.get("removed", 0))

            if self.mode != "mock" and not all(
                (self.target_root / directory).is_dir() for directory in ("etc", "usr")
            ):
                raise BuildOrchestratorError("Target rootfs is missing required etc/usr directories")
            iso_engine = ISOEngine(self.workdir, self.target_root, name, self.config, self.mode, toolchain)
            disk_formats = {"img", "raw", "qcow2", "vmdk", "vhd", "vhdx", "vdi"}

            if self.output_format in disk_formats:
                disk_engine = DiskEngine(self.workdir, self.target_root, name, self.config, self.mode, toolchain=toolchain)
                artifact = disk_engine.build_disk_image(target_format=self.output_format)
            elif self.output_format in {"container", "oci"}:
                container_engine = ContainerEngine(self.target_root, name, self.config, self.mode)
                artifact = container_engine.build_oci_archive()
            elif self.output_format == "tarball":
                artifact = iso_engine.build_tarball()
            elif self.output_format == "netboot":
                artifact = iso_engine.build_netboot_archive()
            else:
                artifact = iso_engine.build_iso()
            self._verify_artifact(artifact)
            hooks.run_stage("post-chroot", artifact=artifact)
            self._verify_artifact(artifact)
            if self.generate_manifest and artifact and artifact.exists():
                self._generate_checksums(artifact, chroot=chroot)
            self._fix_output_permissions(artifact.parent)

            return artifact
        finally:
            try:
                chroot.umount_virtual_fs()
            except Exception:
                logger.exception("Could not unmount target virtual filesystems")
            try:
                toolchain.umount_virtual_fs()
            except Exception:
                logger.exception("Could not unmount build-host virtual filesystems")

            if self.mode != "mock" and os.geteuid() == 0:
                unmount_all_under(resolve_from_project("workdir"))
                if getattr(self, "_tmpfs_mounted", False):
                    subprocess.run(["umount", "-l", str(self.workdir)], check=False, stderr=subprocess.DEVNULL)
                    self._tmpfs_mounted = False

            if self.clean and self.mode != "mock" and self.workdir.exists():
                try:
                    safe_remove_tree(self.workdir, allowed_root=resolve_from_project("workdir"))
                except Exception:
                    logger.exception("Could not safely remove workdir %s", self.workdir)

            # The build host is a disposable runtime root; its verified
            # archive remains in the project-level cache for reuse.
            build_host_dir = getattr(toolchain, "build_host_dir", None)
            if self.clean and self.mode != "mock" and build_host_dir and Path(build_host_dir).exists():
                try:
                    safe_remove_tree(build_host_dir, allowed_root=resolve_from_project("workdir"))
                except Exception:
                    logger.exception("Could not safely remove build_host %s", build_host_dir)

            output_dir = resolve_from_project("output")
            self._fix_output_permissions(output_dir)
            if artifact:
                self._fix_output_permissions(artifact.parent)

    def _verify_artifact(self, artifact: Path):
        if not artifact.is_file() or (self.mode != "mock" and artifact.stat().st_size == 0):
            raise BuildOrchestratorError(f"Build did not produce a non-empty artifact: {artifact}")

    def _fix_output_permissions(self, output_dir: Path):
        """Fix ownership of output directory and built ISOs from root to SUDO_USER if invoked via sudo."""
        if not output_dir.exists():
            return
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if sudo_uid and sudo_gid:
            try:
                uid = int(sudo_uid)
                gid = int(sudo_gid)
                for root, dirs, files in os.walk(output_dir):
                    for d in dirs:
                        try:
                            os.chown(os.path.join(root, d), uid, gid)
                        except Exception:
                            pass
                    for f in files:
                        try:
                            os.chown(os.path.join(root, f), uid, gid)
                        except Exception:
                            pass
                os.chown(output_dir, uid, gid)
                logger.info(f"Updated ownership of {output_dir} to non-root user ({sudo_uid}:{sudo_gid})")
            except Exception as e:
                logger.warning(f"Could not update output ownership: {e}")

    def _generate_checksums(self, artifact_path: Path, chroot: Optional[ChrootManager] = None):
        if not artifact_path or not artifact_path.exists():
            return
        import hashlib
        sha256 = hashlib.sha256()
        md5 = hashlib.md5()
        with open(artifact_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
                md5.update(chunk)

        sha256_path = artifact_path.with_name(f"{artifact_path.name}.sha256")
        md5_path = artifact_path.with_name(f"{artifact_path.name}.md5")
        sha256_path.write_text(f"{sha256.hexdigest()}  {artifact_path.name}\n")
        md5_path.write_text(f"{md5.hexdigest()}  {artifact_path.name}\n")
        self._verify_checksum_file(artifact_path, sha256_path, "sha256")
        self._verify_checksum_file(artifact_path, md5_path, "md5")

        from deb_dev_builder.core.path_utils import resolve_output_path
        manifest_path = resolve_output_path(artifact_path, ".manifest")
        try:
            if chroot and chroot.mode != "mock":
                dpkg_res = chroot.run_in_chroot(["dpkg-query", "-W", "-f=${Package}\t${Version}\n"], check=False, capture_output=True, text=True)
                if dpkg_res.returncode == 0 and dpkg_res.stdout:
                    manifest_path.write_text(dpkg_res.stdout)
                else:
                    manifest_path.write_text(f"# Package manifest for {artifact_path.name}\n")
            else:
                manifest_path.write_text(f"# Package manifest for {artifact_path.name}\n")
        except Exception as e:
            logger.warning(f"Could not write manifest file: {e}")

    @staticmethod
    def _verify_checksum_file(artifact_path: Path, checksum_path: Path, algorithm: str) -> None:
        """Verify a checksum file immediately after it is written."""
        import hashlib

        fields = checksum_path.read_text(encoding="utf-8").strip().split(maxsplit=1)
        if len(fields) != 2 or fields[1].lstrip("*") != artifact_path.name:
            raise BuildOrchestratorError(f"Invalid {algorithm} checksum file: {checksum_path}")
        digest = hashlib.new(algorithm)
        with artifact_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest().lower() != fields[0].lower():
            raise BuildOrchestratorError(f"{algorithm} checksum verification failed for {artifact_path}")
