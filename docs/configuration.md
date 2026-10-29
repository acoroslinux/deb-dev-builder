# Configuration profiles

Project defaults come from `configs/base_customizations.json`; global build
settings come from `configs/global_build.json`. Selected profiles are then
merged in this order: distribution, init system, architecture, variant,
desktop, display-protocol support, kernel, bootloader, base software, requested
software, services, and live user.

Lists are merged without duplicates; dictionaries are merged recursively. JSON
files must contain an object. Profile `packages` entries are normalized to the
runtime `software` list. A variant may use `exclude_packages` after composition;
the `minimal` variant uses this to remove nonessential convenience packages.
Service profiles install their required package as well as enabling the unit.
A selected live-user profile replaces the default group membership, so the
`guest` profile never inherits `sudo`.

| Directory | Purpose | Example |
|---|---|---|
| `configs/architectures` | User and dpkg architecture names | `aarch64` maps to `arm64` |
| `configs/system` | Distribution, init, kernel, and variant | `debian-12`, `openrc`, `generic` |
| `configs/desktops` | Desktop and display manager packages | `xfce` |
| `configs/software` | Optional package groups | `development` |
| `configs/services` | Services to enable or disable | `network` |
| `configs/repos` | Optional user-supplied APT repository profiles | `company` |
| `configs/boot` | Bootloader packages and settings | `grub2-hybrid` |
| `configs/hardware` | Device architecture/format defaults | `rpi4` |
| `configs/vm` | VM format, architecture support and guest packages | `qemu` |
| `configs/live-users` | Live-user name and exact group membership | `guest` |

Desktop profiles declare `session_type` (`x11`, `wayland`, or `both`) so a pure
Wayland build does not install the complete Xorg profile. A profile can declare
`supported_suites`; selecting it on another suite is rejected.

Hardware marked `experimental` is accepted with an explicit warning. The disk
engine supports declarative raw boot-artifact offsets, reserved space before
partitions, U-Boot `extlinux.conf`, and firmware trees. Vendor firmware remains
external: required files must exist in the paths declared by the profile and
can be supplied by a `post-packages` or `post-customize` hook. Missing required
files stop a real build instead of producing a knowingly unbootable image.

Bootable formats automatically include the firmware profile. Rootfs tarballs
and OCI images do not, which keeps container artifacts free of live-media and
hardware-only packages.

Examples:

```bash
python cli.py --list-options
python cli.py aarch64 --distro debian-13 --package-profile development --service-profile network --validate
python cli.py amd64 --distro debian-13 --variant live --live-profile guest --validate
python cli.py --device rpi4 --distro debian-12 --mode mock
python cli.py amd64 --distro debian-13 --vm-profile qemu --validate
```
## Toolchain selection

The global configuration accepts `toolchain.backend` values `auto`,
`isolated-oci` and `isolated-tarball`. Real builds use an isolated build host;
native execution is disabled. `auto` and `isolated-oci` reuse a verified cached
tarball first, then bootstrap from the official Debian OCI image if none exists.
`isolated-tarball` requires a verified local, cached or downloaded tarball.
See [toolchain configuration](toolchain.md) for paths and checksums.

## Additional repositories

Create `configs/repos/company.json` and select it with `--repo-profile company`.
This optional directory is not needed for the built-in distro repositories.
Profiles accept `repos` or `extra_repos`, containing APT source strings or
objects such as:

```json
{
  "repos": [{
    "url": "https://packages.example.org/debian",
    "suite": "trixie",
    "components": ["main"],
    "architectures": ["amd64"],
    "signed_by": "/usr/share/keyrings/company.gpg",
    "key_source": "configs/keys/company.gpg"
  }]
}
```

The local `key_source` file is copied into the target before APT metadata is
updated. Relative key sources resolve from the project root. It requires a
`signed_by` destination below `/usr/share/keyrings` or `/etc/apt/keyrings`.
The builder does not disable authentication for third-party repositories.
Repository profiles accept repeated and comma-separated names, like package
and service profiles.

`systemd-boot` is available as a bootloader profile for disk output. ISO output
uses GRUB2 or syslinux. Disk images use a matching kernel/initramfs pair,
architecture-specific removable UEFI paths and an `/etc/fstab` for the ROOTFS
and EFI labels. Live-only kernel options are removed from disk boot entries.
