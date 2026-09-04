#!/usr/bin/env python3
"""
cli.py — Deb-Dev-Builder Entry Point

Modular Debian and Devuan Linux ISO & Image Builder.
"""

import argparse
import re
import sys
import subprocess
from pathlib import Path

from deb_dev_builder.core.orchestrator import BuildOrchestrator, BuildOrchestratorError
from deb_dev_builder.core.toolchain_manager import ToolchainManagerError
from deb_dev_builder.core.apt_manager import APTManagerError
from deb_dev_builder.core.chroot_manager import ChrootManagerError
from deb_dev_builder.core.iso_engine import ISOEngineError
from deb_dev_builder.core.disk_engine import DiskEngineError
from deb_dev_builder.core.container_engine import ContainerEngineError
from deb_dev_builder.core.config_loader import ConfigLoader, ConfigLoaderError
from deb_dev_builder.core.path_utils import resolve_from_project


def _available_profiles(config_root: Path, category: str, profile_key: str = None):
    category_dir = config_root / category
    if not category_dir.exists() or not category_dir.is_dir():
        return []
    profiles = []
    loader = ConfigLoader(config_root=config_root)
    for path in category_dir.glob("*.json"):
        if profile_key is None or profile_key in loader.load_json(path):
            profiles.append(path.stem)
    return sorted(profiles)


def _slugify_name(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip().lower())
    normalized = normalized.strip("-._")
    return normalized or fallback


def _parse_list_arg(arg_value) -> list:
    if not arg_value:
        return []
    items = []
    if isinstance(arg_value, list):
        for val in arg_value:
            if isinstance(val, list):
                for inner in val:
                    items.extend([x.strip() for x in inner.split(",") if x.strip()])
            elif isinstance(val, str):
                items.extend([x.strip() for x in val.split(",") if x.strip()])
    elif isinstance(arg_value, str):
        items.extend([x.strip() for x in arg_value.split(",") if x.strip()])
    return items


VALID_ARCHS = ("amd64", "x86_64", "i386", "i686", "aarch64", "armhf", "riscv64")


class CustomArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        print(f"❌ Error: {message}", file=sys.stderr)
        sys.exit(2)


def main():
    default_config_path = resolve_from_project("configs/global_build.json")

    parser = CustomArgumentParser(
        description="Deb-Dev-Builder: Modular Debian & Devuan Linux ISO & Image Builder",
        epilog="Use --help to see a detailed list of available arguments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--device",
        type=str,
        help="Hardware device profile (e.g., rpi4, pinebookpro)",
    )
    parser.add_argument(
        "--vm-profile",
        type=str,
        help="Virtual-machine profile (qemu, virtualbox, vmware, or hyperv).",
    )

    parser.add_argument(
        "architecture",
        nargs="?",
        default="amd64",
        choices=list(VALID_ARCHS),
        help="Target architecture (amd64, i386, aarch64, armhf, riscv64). Default: amd64",
    )

    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default=str(default_config_path),
        help="Path to the global configuration JSON file.",
    )

    parser.add_argument(
        "--mode",
        choices=["mock", "real"],
        default="mock",
        help="Execution mode: 'mock' (simulation, no root required) or 'real' (actual build, requires root). Default: mock",
    )

    parser.add_argument(
        "--clean",
        dest="clean",
        action="store_true",
        default=True,
        help="Remove target work directory before real build (default).",
    )
    parser.add_argument(
        "--no-clean",
        dest="clean",
        action="store_false",
        help="Reuse existing real-build work directory.",
    )
    parser.add_argument(
        "--force-isolated-toolchain",
        action="store_true",
        help="Force use of isolated build-host chroot environment.",
    )

    parser.add_argument(
        "--distro",
        type=str,
        default="debian-13",
        help="Distro profile (e.g. debian-13, debian-14, devuan-6, devuan-5). Default: debian-13",
    )

    parser.add_argument(
        "--variant",
        type=str,
        default=None,
        help="Variant profile (live, minimal, server, or iot).",
    )

    parser.add_argument(
        "--init-system",
        type=str,
        default=None,
        help="Init system profile (systemd, sysvinit, openrc, runit). Default: systemd for Debian, sysvinit for Devuan",
    )

    parser.add_argument(
        "-d",
        "--desktop",
        type=str,
        default=None,
        help="Desktop environment profile (gnome, kde, xfce, mate, cinnamon, lxqt, i3, sway, hyprland).",
    )

    parser.add_argument(
        "-k",
        "--kernel",
        type=str,
        default="generic",
        help="Kernel flavor profile: 'generic' (standard desktop/server kernel), 'rt' (real-time PREEMPT_RT kernel), or 'cloud' (minimal virtualized cloud kernel). Default: generic",
    )

    parser.add_argument(
        "-b",
        "--bootloader",
        type=str,
        default=None,
        help="Bootloader profile (grub2-hybrid, grub2-uefi, grub2-bios, syslinux). Default: hybrid on x86 ISO, UEFI otherwise",
    )
    parser.add_argument("--fs-type", type=str, default="ext4", choices=["ext4", "btrfs", "xfs", "f2fs"], help="Root filesystem type (ext4, btrfs, xfs, f2fs). Default: ext4")

    parser.add_argument(
        "-p",
        "--package-profile",
        action="append",
        default=[],
        help="Add package profile from configs/software/.",
    )

    parser.add_argument(
        "--service-profile",
        action="append",
        default=[],
        help="Add service profile from configs/services/ (repeatable or comma-separated).",
    )

    parser.add_argument(
        "--live-profile",
        type=str,
        default=None,
        help="Live-user profile from configs/live-users/ (for example: admin or guest).",
    )

    parser.add_argument(
        "-f",
        "--format",
        choices=["iso", "netboot", "img", "raw", "qcow2", "vmdk", "vhd", "vhdx", "vdi", "tarball", "container", "oci"],
        default="iso",
        help="Output artifact format: iso, netboot, img/raw, qcow2, vdi, vmdk, vhd/vhdx, tarball, or OCI/container. Default: iso",
    )

    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="Output filename for the final build artifact.",
    )

    parser.add_argument(
        "--hostname",
        type=str,
        default=None,
        help="Set custom system hostname.",
    )

    parser.add_argument(
        "--live-user",
        type=str,
        default=None,
        help="Set custom live user name.",
    )

    parser.add_argument(
        "--compression",
        choices=["zstd", "xz", "gzip", "lz4"],
        default=None,
        help="SquashFS compression algorithm. Default: zstd",
    )

    parser.add_argument(
        "--with-calamares",
        action="store_true",
        help="Include Calamares graphical installer on the ISO.",
    )

    parser.add_argument(
        "--with-debian-installer",
        "--with-di",
        dest="with_debian_installer",
        action="store_true",
        help="Include official Debian Installer (d-i) kernel, initrd, and boot menu entries.",
    )

    parser.add_argument(
        "--preseed",
        type=str,
        default=None,
        help="Path or name of custom Debian Installer preseed configuration file (e.g. server, desktop, /path/to/custom.cfg).",
    )

    parser.add_argument(
        "--di-mode",
        choices=["live", "netinstall", "netboot"],
        default="live",
        help="Debian Installer operation mode: 'live' (Live ISO + d-i), 'netinstall' (minimal netinstall ISO), or 'netboot' (PXE/TFTP boot archive). Default: live",
    )

    parser.add_argument(
        "--use-seed",
        dest="use_seed",
        action="store_true",
        default=True,
        help="Use cached minimal rootfs seed tarball for instant bootstrap (< 5s). Enabled by default.",
    )

    parser.add_argument(
        "--no-seed",
        dest="use_seed",
        action="store_false",
        help="Disable rootfs seed tarball caching and force fresh bootstrap from mirror.",
    )

    parser.add_argument(
        "--recreate-seed",
        action="store_true",
        help="Force re-building and re-caching the minimal rootfs seed tarball.",
    )

    parser.add_argument(
        "--multimedia-codecs",
        action="store_true",
        help="Automatically install complete H.264/AAC/FFmpeg multimedia codecs.",
    )

    parser.add_argument(
        "--with-flathub",
        action="store_true",
        help="Configure Flathub Flatpak repository on first boot.",
    )

    parser.add_argument(
        "--with-zram",
        action="store_true",
        help="Configure systemd-zram-generator for RAM compressed swap.",
    )

    parser.add_argument(
        "--with-offline-repo",
        action="store_true",
        help="Embed an offline DEB package repository on the ISO.",
    )

    parser.add_argument(
        "--offline-repo-packages",
        type=str,
        default=None,
        help="Comma-separated list of packages to include in the offline ISO repository.",
    )

    parser.add_argument(
        "--list-options",
        action="store_true",
        help="List all available configuration profiles and exit.",
    )

    parser.add_argument(
        "--validate",
        dest="validate_only",
        action="store_true",
        help="Validate build configuration without performing full build.",
    )

    
    parser.add_argument(
        "--fast",
        "--quick",
        dest="fast_mode",
        action="store_true",
        help="Enable ultra-fast build mode (multi-threaded zstd level 3, fast block sizes, and optimized staging).",
    )

    parser.add_argument(
        "--tmpfs",
        action="store_true",
        help="Mount working directory as tmpfs in RAM for extreme build speed.",
    )

    parser.add_argument(
        "--hooks-dir",
        default=None,
        help="Hook root containing ordered <phase>.d directories. Default: hooks/.",
    )
    parser.add_argument(
        "--no-hooks",
        action="store_true",
        help="Disable all build hooks.",
    )

    args = parser.parse_args()

    if args.di_mode == "netboot":
        args.with_debian_installer = True
        if "--format" not in sys.argv and "-f" not in sys.argv:
            args.format = "netboot"
    elif args.di_mode == "netinstall":
        args.with_debian_installer = True

    if args.vm_profile:
        if args.device:
            parser.error("--device and --vm-profile cannot be combined")
        vm_file = resolve_from_project(f"configs/vm/{args.vm_profile}.json")
        try:
            vm_config = ConfigLoader(resolve_from_project("configs")).load_json(vm_file)
        except ConfigLoaderError as exc:
            parser.error(str(exc))
        if "--format" not in sys.argv and "-f" not in sys.argv:
            args.format = vm_config["output_format"]


    # ── Handle Device Profile ───────────────────────────────────────────────────
    if getattr(args, "device", None):
        device_file = resolve_from_project(f"configs/hardware/{args.device}.json")
        if device_file.exists():
            try:
                dev_cfg = ConfigLoader(config_root=resolve_from_project("configs")).load_json(device_file)
            except ConfigLoaderError as exc:
                parser.error(str(exc))
            if dev_cfg.get("status") == "experimental":
                print(
                    f"⚠️ Hardware profile '{args.device}' is experimental: "
                    f"{dev_cfg.get('notes', 'platform-specific boot files are required')}",
                    file=sys.stderr,
                )
            
            # Explicitly update args.architecture if not provided on CLI
            if "architecture" in dev_cfg:
                # If architecture was not passed in sys.argv (not considering flags for architecture since it is positional usually)
                arch_passed = any(a in VALID_ARCHS for a in sys.argv[1:])
                if not arch_passed or getattr(args, "architecture", "") == "x86_64":
                    args.architecture = dev_cfg["architecture"]
            
            # Explicitly update format
            if "output_format" in dev_cfg and "--format" not in sys.argv and "-f" not in sys.argv:
                args.format = dev_cfg["output_format"]
                
            # Explicitly update bootloader
            if "bootloader" in dev_cfg and "--bootloader" not in sys.argv and "-b" not in sys.argv:
                # To prevent config_loader from crashing when we pass a dict, we can dump it to a temporary file
                # OR we just set args.bootloader = dev_cfg["bootloader"] and fix config_loader.py
                args.bootloader = dev_cfg["bootloader"]
        else:
            parser.error(f"Unknown hardware device profile: {args.device}")
                
    # Map architectures to Debian style
    if hasattr(args, 'architecture'):
        if args.architecture == 'x86_64':
            args.architecture = 'amd64'
        elif args.architecture == 'i686':
            args.architecture = 'i386'


    config_root = resolve_from_project("configs")
    if args.list_options:
        print("Available Deb-Dev-Builder profiles:")
        categories = [
            ("architectures", "architectures", "arch"),
            ("system",       "distros      ", "distro"),
            ("system",       "init-systems ", "init_system"),
            ("desktops",     "desktops     ", "desktop"),
            ("system",       "variants     ", "variant"),
            ("system",       "kernels      ", "kernel"),
            ("boot",         "bootloaders  ", "bootloader"),
            ("software",     "packages     ", None),
            ("services",     "services     ", "services"),
            ("live-users",   "live-users   ", "live_user"),
            ("hardware",     "hardware     ", "architecture"),
            ("vm",           "vm           ", "output_format"),
        ]
        for dir_name, label, profile_key in categories:
            profs = _available_profiles(config_root, dir_name, profile_key)
            print(f"  {label}: {', '.join(profs) if profs else '(none)'}")
        sys.exit(0)

    arch_lower = args.architecture.lower()
    if arch_lower not in VALID_ARCHS:
        print(f"Error: Architecture '{args.architecture}' is not supported.", file=sys.stderr)
        sys.exit(1)

    parsed_package_profiles = _parse_list_arg(args.package_profile)
    parsed_service_profiles = _parse_list_arg(args.service_profile)
    parsed_offline_packages = _parse_list_arg(args.offline_repo_packages)

    try:
        orchestrator = BuildOrchestrator(
            arch=arch_lower,
            config_path=args.config,
            mode=args.mode,
            clean=args.clean,
            distro=args.distro,
            init_system=args.init_system,
            desktop=args.desktop,
            kernel=args.kernel,
            fs_type=args.fs_type,
            bootloader=args.bootloader,
            variant=args.variant,
            package_profiles=parsed_package_profiles,
            service_profiles=parsed_service_profiles,
            live_profile=args.live_profile,
            output_format=args.format,
            with_calamares=args.with_calamares,
            with_debian_installer=args.with_debian_installer,
            preseed=args.preseed,
            di_mode=args.di_mode,
            use_seed=args.use_seed,
            recreate_seed=args.recreate_seed,
            multimedia_codecs=args.multimedia_codecs,
            with_flathub=args.with_flathub,
            with_zram=args.with_zram,
            with_offline_repo=args.with_offline_repo,
            offline_repo_packages=parsed_offline_packages,
            force_isolated_toolchain=args.force_isolated_toolchain,
            compression=args.compression,
            hostname=args.hostname,
            live_user_name=args.live_user,
            fast_mode=getattr(args, "fast_mode", False),
            use_tmpfs=getattr(args, "tmpfs", False),
            hooks_dir=args.hooks_dir,
            hooks_enabled=not args.no_hooks,
            hardware_profile=args.device,
            vm_profile=args.vm_profile,
        )
    except (ConfigLoaderError, BuildOrchestratorError) as exc:
        print(f"❌ Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.validate_only:
        print(f"\n🔍 Validating configuration for '{arch_lower}' / '{args.distro}'...")
        report = orchestrator.validate()
        if report.get("valid"):
            print("✅ Configuration is VALID!")
        else:
            print("❌ Configuration ERRORS:", report.get("errors"))
        sys.exit(0 if report.get("valid") else 1)

    print(f"🚀 Starting Deb-Dev-Builder [{args.mode.upper()} MODE] for {arch_lower} ({args.distro})...")
    try:
        artifact = orchestrator.build(output_name=args.output)
    except (BuildOrchestratorError, ToolchainManagerError, APTManagerError, ChrootManagerError, ISOEngineError, DiskEngineError, ContainerEngineError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"❌ Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"🎉 Build completed successfully! Output: {artifact}")


if __name__ == "__main__":
    main()
