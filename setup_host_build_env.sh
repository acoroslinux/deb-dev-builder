#!/bin/bash
# Deb-Dev-Builder: Host Build Environment Setup
set -e

if [ "$EUID" -ne 0 ]; then 
    echo "Please run with sudo."
    exit 1
fi

echo "Detecting package manager..."
if command -v apt >/dev/null 2>&1; then
    echo "Installing via apt..."
    apt update
    apt install -y mmdebstrap debootstrap qemu-user-static binfmt-support xorriso squashfs-tools syslinux-common mtools dosfstools grub-common grub-efi-amd64-bin grub-efi-ia32-bin grub-pc-bin
elif command -v dnf >/dev/null 2>&1; then
    echo "Installing via dnf..."
    dnf install -y debootstrap qemu-user-static binfmt-support xorriso squashfs-tools syslinux mtools dosfstools grub2-tools grub2-efi-x64-modules grub2-efi-ia32-modules grub2-pc-modules
elif command -v pacman >/dev/null 2>&1; then
    echo "Installing via pacman..."
    pacman -Sy --noconfirm debootstrap qemu-user-static-binfmt xorriso squashfs-tools syslinux mtools dosfstools grub
elif command -v emerge >/dev/null 2>&1; then
    echo "Installing via emerge (Gentoo)..."
    GRUB_PLATFORMS="efi-64 efi-32 pc" emerge -uN dev-util/debootstrap app-emulation/qemu sys-fs/squashfs-tools dev-libs/libisoburn sys-boot/syslinux sys-boot/grub
elif command -v xbps-install >/dev/null 2>&1; then
    echo "Installing via xbps..."
    xbps-install -Sy debootstrap qemu-user-static binfmt-support xorriso squashfs-tools syslinux mtools dosfstools grub-x86_64-efi grub-i386-efi
else
    echo "Unsupported package manager. Please install mmdebstrap/debootstrap, qemu-user-static, xorriso, squashfs-tools manually."
    exit 1
fi

echo "Enabling binfmt services..."
if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now systemd-binfmt.service || true
fi

echo "Host build environment setup complete!"
