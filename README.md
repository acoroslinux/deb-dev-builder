# Deb-Dev-Builder

**Modular and Dynamic Debian & Devuan Linux ISO & Image Builder**

`deb-dev-builder` is a Python-based build system for creating customized Debian & Devuan Linux live ISOs, raw disk images, rootfs tarballs, and OCI container images. It follows the same modular, profile-driven architecture as its sibling builders (`gentoo-builder`, `fedora-builder`, `arch-builder`, `void-builder`) while embracing Debian & Devuan ecosystems (`mmdebstrap`, `debootstrap`, `dpkg`, `apt`, `live-boot`, `live-config`, `systemd`, `sysvinit`, `openrc`, `runit`, `s6`).

---

## Features

- 🎯 **Profile-Driven**: JSON profiles for distros, init systems, desktops, packages, services, repos, releases, variants, kernels, and bootloaders
- 🐧 **Debian & Devuan Native**: Uses `mmdebstrap` / `debootstrap` for ultra-fast rootfs bootstrap
- ⚙️ **Init System Independence**: Support for `systemd`, `sysvinit`, `openrc`, `runit`, and `s6`
- 🏛️ **Multi-Architecture**: `amd64`, `i386`, `aarch64`, `armhf`, `riscv64`
- 🔒 **Secure Boot Ready**: `shim-signed` + GRUB2 EFI with GPT hybrid partitioning
- 📦 **Flathub & ZRAM Ready**: Automatic systemd-zram-generator and Flathub flatpak integration
- 🎨 **Desktop Environments**: GNOME, KDE, XFCE, MATE, Cinnamon, LXQt, i3, Sway, Hyprland
- 💿 **Calamares Installer**: Integrated GUI installer launcher
- 🔍 **Mock Mode**: Full build simulation without root privileges

---

## Quick Start

```bash
# Simulate a Debian 12 GNOME ISO build (no root required)
python cli.py amd64 --distro debian-12 --desktop gnome --mode mock

# Build a real Devuan 5 OpenRC XFCE ISO (requires root)
sudo python cli.py amd64 --distro devuan-5 --init-system openrc --desktop xfce --mode real

# Build Debian 13 with Calamares and multimedia codecs
sudo python cli.py amd64 --distro debian-13 --desktop kde --with-calamares --multimedia-codecs --mode real

# List all available profiles
python cli.py --list-options

# Validate configuration without building
python cli.py amd64 --distro debian-12 --validate
```