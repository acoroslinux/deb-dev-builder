# Deb-Dev-Builder

**Modular and Dynamic Debian & Devuan Linux ISO & Image Builder**

`deb-dev-builder` is a Python-based build system for creating customized Debian & Devuan Linux live ISOs, raw disk images, rootfs tarballs, and OCI container images. It follows the same modular, profile-driven architecture as its sibling builders (`gentoo-builder`, `fedora-builder`, `arch-builder`, `void-builder`) while embracing Debian & Devuan ecosystems (`mmdebstrap`, `debootstrap`, `dpkg`, `apt`, `live-boot`, `live-config`, `systemd`, `sysvinit`, `openrc`, `runit`).

---

## Features

- 🎯 **Profile-Driven**: JSON profiles for distros, init systems, desktops, packages, services, repos, releases, variants, kernels, and bootloaders
- 🐧 **Debian & Devuan Native**: Uses `mmdebstrap` / `debootstrap` for ultra-fast rootfs bootstrap
- ⚙️ **Init System Independence**: Support for `systemd`, `sysvinit`, `openrc`, and `runit`
- 🏛️ **Multi-Architecture**: `amd64`, `i386`, `aarch64`, `armhf`, `riscv64`
- 🔒 **Secure Boot Aware**: Debian x86 builds include signed shim/GRUB assets when the selected repositories provide them
- 📦 **Flathub & ZRAM Ready**: Automatic systemd-zram-generator and Flathub flatpak integration
- 🎨 **Desktop Environments**: GNOME, KDE, XFCE, MATE, Cinnamon, LXQt, i3, Sway, Hyprland
- 💿 **Calamares Installer**: Integrated GUI installer launcher
- 💾 **Multiple Artifacts**: ISO, raw IMG, QCOW2, VDI, VMDK, rootfs tarball, and OCI image-layout archive
- 🌐 **Install Media**: Live+d-i, installer-only netinstall ISO, PXE netboot archive, and Calamares live installer
- 🖥️ **VM Profiles**: QEMU/KVM, VirtualBox, VMware, and Hyper-V formats with guest integration packages
- 🪝 **Build Hooks**: Ordered hooks around bootstrap, packages, customization, artifact creation, errors, and cleanup
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

## Documentation

The complete HTML manual is built from [`docs/`](docs/index.rst):

```bash
python3 -m pip install -r docs/requirements.txt
sphinx-build -W -b html docs docs/_build/html
```

See the manual for the [build pipeline](docs/build-pipeline.md), [configuration profiles](docs/configuration.md), [artifact formats](docs/artifacts.md), and [troubleshooting](docs/troubleshooting.md).
