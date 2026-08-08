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
from deb_dev_builder.core.config_loader import ConfigLoader
from deb_dev_builder.core.path_utils import resolve_from_project
import logging

logger = logging.getLogger("orchestrator")

class BuildOrchestratorError(Exception):
    pass

class BuildOrchestrator:
    def __init__(
        self,
        arch: str = "amd64",
        config_path: str = "configs/global_build.json",
        mode: str = "mock",
        clean: bool = True,
        distro: Optional[str] = "debian-12",
        init_system: Optional[str] = "systemd",
        desktop: Optional[str] = None,
        kernel: Optional[str] = "kernel",
        bootloader: Optional[str] = "grub2-hybrid",
        variant: Optional[str] = "live",
        package_profiles: Optional[List[str]] = None,
        service_profiles: Optional[List[str]] = None,
        repo_profiles: Optional[List[str]] = None,
        live_profile: Optional[str] = None,
        live_user: Optional[str] = None,
        live_groups: Optional[List[str]] = None,
        output_format: str = "iso",
        compression: str = "zstd",
        generate_manifest: bool = True,
        with_calamares: bool = False,
        multimedia_codecs: bool = False,
        with_flathub: bool = False,
        with_zram: bool = False,
        force_isolated_toolchain: bool = False,
    ):
        self.arch = arch
        self.config_path = config_path
        self.mode = mode
        self.clean = clean
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
        self.live_user = live_user
        self.live_groups = live_groups or []
        self.output_format = output_format
        self.compression = compression
        self.generate_manifest = generate_manifest
        self.with_calamares = with_calamares
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
        self.config["with_calamares"] = self.with_calamares
        self.config["with_flathub"] = self.with_flathub
        self.config["with_zram"] = self.with_zram

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

        chroot = ChrootManager(self.target_root, self.mode, arch=self.arch)
        try:
            toolchain.mount_virtual_fs()
            chroot.mount_virtual_fs()

            apt = APTManager(chroot, self.config, toolchain=toolchain)
            suite = self.config.get("suite", "bookworm")
            apt.bootstrap_rootfs(suite, self.arch)
            apt.configure_sources_list()
            apt.update_apt_cache()

            pkgs = self.config.get("packages", [])
            apt.install_packages(pkgs)

            customizer = SystemCustomizer(chroot, self.config)
            customizer.configure_live_environment()

            chroot.umount_virtual_fs()

            iso_engine = ISOEngine(self.workdir, self.target_root, name, self.config, self.mode, toolchain)
            if self.output_format == "img":
                disk_engine = DiskEngine(self.workdir, self.target_root, name, self.config, self.mode)
                artifact = disk_engine.build_disk_image()
            else:
                artifact = iso_engine.build_iso()

            if self.generate_manifest and artifact and artifact.exists():
                self._generate_checksums(artifact)

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

    def _generate_checksums(self, artifact_path: Path):
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
