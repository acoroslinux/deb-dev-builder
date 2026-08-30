import os
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
from deb_dev_builder.core.path_utils import resolve_from_project, unmount_all_under
import logging

logger = logging.getLogger("orchestrator")

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
    ):
        self.arch = arch
        self.config_path = config_path
        self.distro = distro
        self.init_system = init_system
        self.desktop = desktop
        self.kernel = kernel
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

        # --- SMART BOOTLOADER DEFAULTS ---
        if not self.bootloader:
            if self.output_format == "iso":
                self.bootloader = {"type": "grub2-hybrid"}
            elif getattr(self, "arch", "") in ("aarch64", "arm64"):
                self.bootloader = {"type": "systemd-boot"}  # Default ARM UEFI
            else:
                self.bootloader = {"type": "grub2-uefi"}
                
        # We don't inject extra packages here because debian uses package profiles
        # But we could ensure grub-efi is installed.
        # ---------------------------------

        if self.multimedia_codecs and "multimedia" not in self.package_profiles:
            self.package_profiles.append("multimedia")
        if self.with_offline_repo and "offline-repo" not in self.package_profiles:
            self.package_profiles.append("offline-repo")

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
        )
        selected_bootloader = self.bootloader or self.config.get("bootloader", {}).get("type") or "grub2-hybrid"
        self.config["bootloader"] = {"type": selected_bootloader}
        self.config["bootloader_type"] = selected_bootloader
        self.config["fs_type"] = self.fs_type
        self.config["with_calamares"] = self.with_calamares
        self.config["with_debian_installer"] = self.with_debian_installer
        self.config["preseed"] = self.preseed
        self.config["di_mode"] = self.di_mode
        self.config["with_flathub"] = self.with_flathub
        self.config["with_zram"] = self.with_zram

        if self.output_format == "iso":
            essential_boot_pkgs = [
                "live-boot", "live-config", "live-config-systemd", "systemd-sysv",
                "grub-pc-bin", "grub-efi-amd64-bin", "grub-efi-ia32-bin", "shim-signed",
                "isolinux", "syslinux-common", "dosfstools", "mtools", "efibootmgr"
            ]
        else:
            essential_boot_pkgs = [
                "systemd-sysv", "dosfstools", "mtools", "efibootmgr", "initramfs-tools"
            ]
            if selected_bootloader == "systemd-boot":
                essential_boot_pkgs.append("systemd-boot")
            if self.fs_type == "btrfs":
                essential_boot_pkgs.append("btrfs-progs")
            elif self.fs_type == "f2fs":
                essential_boot_pkgs.append("f2fs-tools")
            elif self.fs_type == "xfs":
                essential_boot_pkgs.append("xfsprogs")
        for pkg in essential_boot_pkgs:
            if pkg not in self.config.get("software", []):
                self.config.setdefault("software", []).append(pkg)

    def validate(self) -> Dict[str, Any]:
        errors = []
        if not self.distro:
            errors.append("Distro profile not specified.")
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
        name = output_name or f"deb-dev-{self.distro}-{self.arch}"

        if self.clean and self.mode != "mock":
            if os.geteuid() == 0:
                unmount_all_under(resolve_from_project("workdir"))
            if self.workdir.exists():
                import shutil
                shutil.rmtree(self.workdir, ignore_errors=True)

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


        if not hasattr(self, "config"):
            self.config = {}
        if hasattr(self.config, "_data"):
            self.config._data["fast_mode"] = self.fast_mode
            self.config._data["use_tmpfs"] = self.use_tmpfs
        elif isinstance(self.config, dict):
            self.config["fast_mode"] = self.fast_mode
            self.config["use_tmpfs"] = self.use_tmpfs

        toolchain = ToolchainManager(
            workdir_base=self.workdir,
            mode=self.mode,
            force_isolated=self.force_isolated_toolchain,
            target_arch=self.arch,
            distro=self.distro,
        )
        toolchain.setup()

        chroot = ChrootManager(self.target_root, self.mode, cache_dir=resolve_from_project(f"cache/{self.arch}"), arch=self.arch)
        try:
            toolchain.mount_virtual_fs()
            chroot.mount_virtual_fs()

            apt = APTManager(chroot, self.config, toolchain=toolchain)
            suite = self.config.get("suite", "bookworm")
            apt.bootstrap_rootfs(suite, self.arch, use_seed=self.use_seed, recreate_seed=self.recreate_seed, reuse_existing=not self.clean)
            apt.configure_sources_list()
            apt.update_apt_cache()

            pkgs = self.config.get("software", [])
            if "zram-tools" not in pkgs: pkgs.append("zram-tools")
            apt.install_packages(pkgs)

            # Prepare offline package repository if requested
            offline_pkgs = list(self.config.get("offline_repo_packages", []))
            if self.offline_repo_packages:
                for p in self.offline_repo_packages:
                    if p not in offline_pkgs:
                        offline_pkgs.append(p)

            if (self.with_offline_repo or offline_pkgs) and self.output_format == "iso":
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
                        grub_target = "arm64-efi" if self.arch in ("aarch64", "arm64") else "x86_64-efi"
                        chroot.run_in_chroot(["grub-install", f"--target={grub_target}", "--efi-directory=/boot/efi", "--bootloader-id=debian", "--removable"], check=False)
                        chroot.run_in_chroot(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"], check=False)
                    elif "systemd-boot" in btype:
                        print(f"\n[ORCHESTRATOR] Installing systemd-boot Bootloader ({btype}) into chroot /boot/efi...")
                        chroot.run_in_chroot(["mkdir", "-p", "/boot/efi/EFI"])
                        chroot.run_in_chroot(["bootctl", "install", "--esp-path=/boot/efi"], check=False)
                else:
                    print(f"\n[MOCK] Simulated bootloader installation: {btype}")
            # ----------------------------------------------------

            chroot.umount_virtual_fs()

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
            else:
                artifact = iso_engine.build_iso()

            if self.generate_manifest and artifact and artifact.exists():
                self._generate_checksums(artifact, chroot=chroot)

            output_dir = resolve_from_project("output")
            self._fix_output_permissions(output_dir)

            return artifact
        finally:
            if self.clean and self.mode != "mock":
                if os.geteuid() == 0:
                    unmount_all_under(resolve_from_project("workdir"))
                if hasattr(self, 'workdir') and self.workdir and self.workdir.exists():
                    import shutil
                    shutil.rmtree(self.workdir, ignore_errors=True)

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


            try:
                chroot.umount_virtual_fs()
            except Exception:
                pass
            try:
                toolchain.umount_virtual_fs()
            except Exception:
                pass

            if self.mode != "mock" and os.geteuid() == 0:
                unmount_all_under(resolve_from_project("workdir"))

            output_dir = resolve_from_project("output")
            self._fix_output_permissions(output_dir)

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

        manifest_path = artifact_path.with_name(f"{artifact_path.stem}.manifest")
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
