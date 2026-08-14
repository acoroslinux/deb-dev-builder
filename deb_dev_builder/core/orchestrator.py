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
        variant: Optional[str] = None,
        package_profiles: Optional[List[str]] = None,
        service_profiles: Optional[List[str]] = None,
        repo_profiles: Optional[List[str]] = None,
        live_profile: Optional[str] = None,
        output_format: str = "iso",
        mode: str = "mock",
        clean: bool = True,
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
        force_isolated_toolchain: bool = False,
    ):
        self.arch = arch
        self.config_path = config_path
        self.distro = distro
        self.init_system = init_system
        self.desktop = desktop
        self.kernel = kernel
        self.bootloader = bootloader
        self.variant = variant
        self.package_profiles = package_profiles or []
        self.service_profiles = service_profiles or []
        self.repo_profiles = repo_profiles or []
        self.live_profile = live_profile
        self.output_format = output_format.lower()
        self.mode = mode.lower()
        self.clean = clean
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
        self.force_isolated_toolchain = force_isolated_toolchain

        if self.multimedia_codecs and "multimedia" not in self.package_profiles:
            self.package_profiles.append("multimedia")

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
        self.config["with_calamares"] = self.with_calamares
        self.config["with_debian_installer"] = self.with_debian_installer
        self.config["preseed"] = self.preseed
        self.config["di_mode"] = self.di_mode
        self.config["with_flathub"] = self.with_flathub
        self.config["with_zram"] = self.with_zram

        essential_boot_pkgs = [
            "live-boot", "live-config", "live-config-systemd", "systemd-sysv",
            "grub-pc-bin", "grub-efi-amd64-bin", "grub-efi-ia32-bin", "shim-signed",
            "isolinux", "syslinux-common", "dosfstools", "mtools", "efibootmgr"
        ]
        for pkg in essential_boot_pkgs:
            if pkg not in self.config.get("packages", []):
                self.config.setdefault("packages", []).append(pkg)

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
                shutil.rmtree(self.workdir, ignore_errors=True)

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

            pkgs = self.config.get("packages", [])
            apt.install_packages(pkgs)

            customizer = SystemCustomizer(chroot, self.config)
            customizer.configure_live_environment()

            chroot.umount_virtual_fs()

            iso_engine = ISOEngine(self.workdir, self.target_root, name, self.config, self.mode, toolchain)
            disk_formats = {"img", "raw", "qcow2", "vmdk", "vhd", "vhdx", "vdi"}

            if self.output_format in disk_formats:
                disk_engine = DiskEngine(self.workdir, self.target_root, name, self.config, self.mode)
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
