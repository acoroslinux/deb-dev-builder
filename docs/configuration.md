# Configuration profiles

Operational settings are distributed by responsibility. `configs/catalog.json`
is a loading index containing paths, not another copy of the settings. The
former monolithic `runtime.json` is no longer needed.

| Source | Responsibility |
|---|---|
| `build/cli.json`, `build/policy.json` | CLI defaults, automatic profiles, feature packages and required tools |
| `build/artifact-naming.json` | Artifact naming template, image/installer labels and UTC build timestamp format |
| `debian-installer/media.json` | Installer sources, live component repository, preseeding and target cleanup policy |
| `architectures/*.json` | Architecture aliases, kernel suffix, EFI targets, image packages and optional bootable-suite/kernel-flavor restrictions |
| `system/generic.json`, `rt.json`, `cloud.json` | Kernel package templates |
| `system/systemd.json`, `sysvinit.json`, `openrc.json`, `runit.json` | Init packages, live-config and zram package selection |
| `system/defaults.json`, `global_build.json` | System defaults and build overrides |
| `system/<distro>.json` | Repository suite, mirrors and components; APT fallbacks use the default distro profile |
| `boot/policy.json`, `boot/<loader>.json` | Boot defaults, image dependencies and selectable loader packages |
| `live-users/*.json` | Account credentials and groups; `policy.json` owns shell and permissions |
| `services/*.json` | Selectable service profiles; `policy.json` owns automatic services and masks |
| `customization/session-audio.json` | Per-user PipeWire startup for sessions without systemd |
| `desktops/session-defaults.json`, `display-managers/*.json` | Session detection, PAM fallback and manager-specific templates |
| `customization/*.json` | One policy per subject: zram, Samba, Flathub, branding, themes, overlays and polkit |
| `customization/offline-repository.json` | Live APT source URI/template and installer source backup paths |
| `images/*.json` | Separate SquashFS, ISO and disk settings |
| `toolchain/defaults.json` | Isolated build host, dependency packages and pinned keyring metadata |

Paths in this table are relative to `configs/`. Files listed as module documents
in the catalog are excluded from selectable profile lists and from profile-role
validation. The architecture and kernel lookup maps are built from the actual
profiles, so changing a suffix or EFI target has a single source of truth.

`custom_files/` remains the target filesystem overlay. Native GTK, GRUB,
Plymouth, Calamares, preseed and shell files keep the formats required by their
consumers. They are not duplicated into JSON. `assets/theme_mapping.json`
continues to map shared artwork to its installed locations. Installer settings
are independent of live-image account and disk-image defaults.

Override a subset in the `runtime` object of the global JSON selected by
`--config`. Objects merge recursively; lists inside `runtime` **replace** the
default list, and `[]` explicitly removes it. For example:

```json
{
  "runtime": {
    "build": {
      "default_live_profile": "liveuser",
      "desktop_update_profiles": []
    },
    "services": {
      "auto_enable": ["NetworkManager", "cups"]
    },
    "artifacts": {
      "squashfs": {"compression": "xz", "block_size": "1M"},
      "disk": {"efi_size_mib": 512}
    }
  }
}
```

This fragment is added alongside the usual `system`, `boot`, `hooks`, `cleanup`
and `toolchain` settings. CLI options and selected profiles keep their existing
precedence over runtime fallbacks. CLI defaults are read from the selected
global JSON before parsing the remaining options. Automatic profile selection
uses the global runtime policy; profile package lists still compose normally.

Display-manager templates accept `{live_user}`, `{session}` and
`{display_server}`. Only the selected manager is configured, and an existing
packaged PAM policy is preserved. Account names, passwords and exact group
membership remain in `configs/live-users/*.json`; changing a runtime policy does
not copy the default account's password into an explicitly selected account.

Project defaults come from `configs/base_customizations.json`; global build
settings come from `configs/global_build.json`. Selected profiles are then
merged in this order: distribution, init system, architecture, variant,
desktop, display-protocol support, kernel, bootloader, base software, requested
software, services, and live user.

The default live account comes from `configs/live-users/liveuser.json`, loaded
before global customization settings. A named `--live-profile` is applied last;
it uses its own credentials and exact group membership.

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

Live and d-i login share the desktop session candidates declared in
`desktops/session-defaults.json`. They select an installed candidate such as
`plasma` or `plasmax11`, with an explicit `desktop_session` taking precedence.
SDDM's autologin session includes the `.desktop` extension in both environments.
See [desktop validation](desktop-validation.md) for the test matrix and the
difference between configuration checks and confirmed VM behavior.
The [desktop catalog](desktop-catalog.md) lists all 66 profile names and their
suite limits, including native shell sessions and standalone window managers.

Desktop profiles can also declare `distribution_overrides`, keyed by distribution
family (for example `devuan`) or release profile (for example `devuan-6`). Each
entry accepts `packages`, `exclude_packages`, `display_manager`, `session_type`,
`backports_mirror`, `backports_suite` and `apt_target_release`; family settings are applied before
release settings. The target release applies to the package-install command,
not a persistent global APT default. Stable Hyprland uses this to resolve its
backports dependencies. XFCE on Devuan selects `xfce-polkit`, or
`policykit-1-gnome` on Daedalus, and excludes `mate-polkit`/`lxpolkit` to avoid
competing authentication agents. This override does not apply to Debian or
other desktops.
Budgie uses session overrides to follow its upstream transition from X11 to
Wayland on newer suites, including the corresponding display manager and Labwc.


Hardware marked `experimental` is accepted with an explicit warning. The disk
engine supports declarative raw boot-artifact offsets, reserved space before
partitions, U-Boot `extlinux.conf`, and firmware trees. Vendor firmware remains
external: required files must exist in the paths declared by the profile and
can be supplied by a `configs/hooks/chroot/` script. Missing required
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

Live boot is selected by the ISO kernel command line (`boot=live`). The builder
must not persist `BOOT=live` in `/etc/initramfs-tools/conf.d/`: Calamares copies
that directory to the installed system, then removes the live-boot backend.
An installed initramfs forced into live mode would source a missing
`/scripts/live` and terminate PID 1. Reused build roots have the builder's old
`live.conf` override removed before initramfs regeneration.

Calamares shortcut policy is owned by `configs/customization/calamares.json`.
The generated session helper checks the configured installer executable and the
live kernel option before writing anything. It uses the current user's XDG
Desktop directory, with a configurable fallback, and does not assume a login
name. Installer icons are no longer seeded into `/etc/skel` or unrelated home
directories. The launchers also declare `TryExec`.

Both init variants schedule `shellprocess@cleanup-live-installer` after user
creation, package removal and bootloader setup, immediately before unmounting.
Its generated configuration removes the target's installer autostart, helper,
menu entry, polkit rule and generated desktop icons (including localized home
directories and old skeleton copies). Every command is rooted in Calamares'
`${ROOT}` target; a guard rejects an empty target or `/`. The live session's
installer remains available. Existing installed systems need a one-time removal
of those old artifacts; the new build policy is not applied retroactively.


## Audio sessions without systemd

`configs/customization/session-audio.json` installs an XDG autostart entry and a
user-session launcher for the selected non-systemd inits, when the PipeWire,
WirePlumber and pipewire-pulse binaries are installed. It waits for the core
socket before starting its clients. Commands, startup checks, retry limits and
paths come from JSON. The launcher requires the user-owned `XDG_RUNTIME_DIR`
provided by the login session, prevents duplicate launches with a per-user lock,
and stops its children when it exits. The lock descriptor is not inherited by
the audio processes. Systemd builds continue to use the packaged user units.

The generated audio startup remains on the installed system. It is a user
service, not a root system service. Verification used the native PipeWire 1.4.2
stack, a private D-Bus session, a virtual sink and a PulseAudio client stream,
including a second launch and a stop/restart cycle. Playback through guest
hardware still needs checking in the rebuilt ISO.

Overlay file copies replace destination symlinks such as Devuan's XFCE
`xsettings.xml` and `xfwm4.xml` alternatives links. Parent links are interpreted
relative to the target rootfs (including merged `/usr` layouts); a destination
must not resolve above that root. This lets the same desktop defaults apply
on Debian and Devuan without writing through absolute links into the host.
