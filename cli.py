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
from deb_dev_builder.core.iso_engine import ISOEngineError
from deb_dev_builder.core.disk_engine import DiskEngineError
from deb_dev_builder.core.container_engine import ContainerEngineError
from deb_dev_builder.core.config_loader import ConfigLoaderError
from deb_dev_builder.core.path_utils import resolve_from_project


def _available_profiles(config_root: Path, category: str):
    category_dir = config_root / category
    if not category_dir.exists() or not category_dir.is_dir():
        return []
    return sorted([p.stem for p in category_dir.glob("*.json")])


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
        "architecture",
        nargs="?",
        default="amd64",
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
        default="debian-12",
        help="Distro profile (e.g. debian-12, debian-13, devuan-5, devuan-6). Default: debian-12",
    )

    parser.add_argument(
        "--variant",
        type=str,
        default=None,
        help="Variant profile (e.g. live, minimal, developer, cloud, hardened).",
    )

    parser.add_argument(
        "--init-system",
        type=str,
        default="systemd",
        help="Init system profile (systemd, sysvinit, openrc, runit, s6). Default: systemd",
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
        default="grub2-hybrid",
        help="Bootloader profile (grub2-hybrid, grub2-uefi, grub2-bios, syslinux). Default: grub2-hybrid",
    )

    parser.add_argument(
        "-p",
        "--package-profile",
        action="append",
        default=[],
        help="Add package profile from configs/packages/.",
    )

    parser.add_argument(
        "-f",
        "--format",
        choices=["iso", "img", "raw", "qcow2", "vmdk", "vhd", "vdi", "tarball", "container"],
        default="iso",
        help="Output artifact format: iso, img, qcow2, vmdk, vhd, vdi, tarball, container. Default: iso",
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

    args = parser.parse_args()

    config_root = resolve_from_project("configs")
    if args.list_options:
        print("Available Deb-Dev-Builder profiles:")
        categories = [
            ("architectures", "architectures"),
            ("distros",       "distros      "),
            ("init-systems",  "init-systems "),
            ("desktops",      "desktops     "),
            ("kernels",       "kernels      "),
            ("bootloaders",   "bootloaders  "),
            ("packages",      "packages     "),
            ("services",      "services     "),
            ("repos",         "repos        "),
        ]
        for dir_name, label in categories:
            profs = _available_profiles(config_root, dir_name)
            print(f"  {label}: {', '.join(profs) if profs else '(none)'}")
        sys.exit(0)

    arch_lower = args.architecture.lower()
    if arch_lower not in VALID_ARCHS:
        print(f"Error: Architecture '{args.architecture}' is not supported.", file=sys.stderr)
        sys.exit(1)

    parsed_package_profiles = _parse_list_arg(args.package_profile)

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
            bootloader=args.bootloader,
            variant=args.variant,
            package_profiles=parsed_package_profiles,
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
            force_isolated_toolchain=args.force_isolated_toolchain,
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
    except (BuildOrchestratorError, ToolchainManagerError, APTManagerError, ISOEngineError, DiskEngineError, ContainerEngineError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"❌ Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"🎉 Build completed successfully! Output: {artifact}")


if __name__ == "__main__":
    main()
