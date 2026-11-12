# Desktop baseline and validation

Validate each desktop on a fresh VM configured for Debian/Linux. The previous
VM configured as Windows 11 produced silent media playback; rebuilding the VM
as Debian restored audio on both Debian and Devuan without changing the ISOs.
A passing configuration check does not establish package availability, a
working graphical session, or a successful installation.

## Current runtime evidence

| Desktop | Login manager | Session profile | Debian 13 / systemd | Devuan 6 / SysVinit |
|---|---|---|---|---|
| XFCE | LightDM | X11 | Working in manual tests | Working in manual tests |
| KDE Plasma | SDDM | X11 and Wayland | Installation and encrypted Btrfs boot confirmed; single-prompt unlock pending | Live Wayland and installed encrypted Btrfs desktop reported; single-prompt unlock pending |
| GNOME | GDM | X11 and Wayland | Live session, network and audio confirmed | Live session, network and audio confirmed; installed encrypted boot and Plymouth prompt confirmed |
| MATE | LightDM | X11 | Pending | Pending |
| Cinnamon | LightDM | X11 | See installation evidence below | See installation evidence below |
| LXQt | SDDM | X11 | Pending | Pending |
| i3 | LightDM | X11 | Pending | Pending |
| Sway | SDDM | Wayland | Pending | Pending |
| Hyprland | SDDM | Wayland | Dependencies verified with backports; VM test pending | Dependencies verified with backports; VM test pending |

The XFCE evidence comes from the user's VM tests: live and installed sessions,
encrypted installation and boot, language settings, autologin, Plymouth,
installation-medium removal, and audible playback. It is not evidence for every
architecture, distribution release, init system, filesystem or installer.
The user also confirmed a Cinnamon installation with autologin, network, audio,
shutdown and reboot. The distribution was not explicitly identified in that
report, so this is not recorded as two separate distribution passes. Cinnamon
encryption remains pending. Devuan's native d-i init selection and Debian's
GRUB configuration-preservation fix have been confirmed in the VM tests.
The native d-i integration now supports both Debian and Devuan profiles.

### Oldstable Calamares compatibility, 2026-11-10

The Debian 12 build stopped during installer auditing because Calamares 3.2.61
rejects the newer `extraMounts[].efi` configuration. The builder now inspects
the installed native module schemas and translates the known 3.2 contracts:
conditional mounts use `extraMountsEfi`, mount options belong to `fstab`, GRUB
uses `keepDistributor`, systemd timers use `timers`, and the GRUB bootloader
configuration supplies the older required kernel/initrd paths. Unsupported
configurations still fail the strict audit; native module files are unchanged.
Recent Calamares configurations retain their existing format.

Contract tests now cover cached native Calamares 3.2.61, 3.3.14 and 3.4.3 packages.
All 46 installer audit tests pass, including execution of the native mount
module for BIOS and UEFI, firmware-variable mount isolation, preservation of
EFI/Btrfs mount policy, and repeated configuration. The full suite passes 622
tests. A fresh oldstable ISO build
and VM installation remain pending. The same capability-based adapter covers
Devuan 5 when its native modules expose the legacy contracts.

### Testing build and live-session evidence, 2026-11-09

Real amd64 XFCE live builds with Calamares completed for both testing families.
Both artifacts passed the builder's structural verification and SHA-256 checks.
They include kernel 7.2.15 and Calamares 3.4.3.

| Distribution | Init stack | ISO build | VM evidence |
|---|---|---|---|
| Debian testing / Forky | systemd 262 | Passed | Live desktop and Calamares launch confirmed below |
| Devuan testing / Freia | SysVinit 3.18, elogind 257.13, eudev 3.3 | Passed | Installed GRUB appearance issue reported; corrected configuration awaiting retest |

The Devuan inventory contains no `systemd`, `systemd-sysv` or `libpam-systemd`
package. Build-time terminal logging exposed a missing devpts mount; private
devpts instances were added to the target and toolchain chroots. The complete
577-test suite passed, and APT terminal logging worked in the real Devuan build.
Build reports retain their build-time runtime-test flags; user VM evidence is
recorded separately here.

The user booted the newly built Debian testing XFCE ISO in VirtualBox. The
screenshot confirms XFCE 4.20 on X11 with kernel 7.2.15+deb14-amd64 and the
Calamares welcome screen displaying Portuguese localization. This establishes
live desktop startup and installer launch for that artifact; network, audio,
installation and installed-system boot remain pending for this testing build.

### Unstable build evidence, 2026-11-10

Real amd64 XFCE live builds with Calamares completed for Debian Sid and Devuan
Ceres. Both artifacts passed structural verification and SHA-256 checks. Their
inventories contain kernel 7.3.3, XFCE session 4.20.4 and Calamares 3.4.3.

| Distribution | Init stack | ISO build | VM evidence |
|---|---|---|---|
| Debian Sid | systemd 262.2 | Passed | Live session, encrypted Calamares installation, unlock and installed XFCE confirmed |
| Devuan Ceres | SysVinit 3.18-2devuan1, elogind 257.13, eudev 3.3 | Passed | Encrypted Calamares installation, single unlock prompt, GRUB, LightDM, XFCE wallpaper and APT confirmed after corrections |

The Ceres inventory contains no `systemd`, `systemd-sysv` or `libpam-systemd`
package. The user initially reported both unstable live sessions working, with
the missing Ceres wallpaper as the sole observed issue. The Sid screenshot shows wallpaper,
XFCE 4.20 on X11, kernel 7.3.3 and VLC playback; a second screenshot shows
Calamares installation at 7%. A subsequent Ceres screenshot confirms completed
installation and reboot into the installed UEFI GRUB menu. Further screenshots
confirm successful cryptsetup unlock, login and the installed XFCE session, with
encrypted ext4 root and encrypted swap shown in GParted. The wallpaper initially
appears during login, then becomes black in the desktop session. Subsequent Sid
screenshots and the user's report confirm successful encrypted installation,
unlock and installed XFCE operation. Its GRUB menu also retains the generic
`GNU/Linux` label, covered by the shared branding correction below.

The Ceres ISO contains its native wallpaper but lacks the default path compiled
into xfdesktop: `/usr/share/images/desktop-base/default`. The
[Debian package build rule](https://sources.debian.org/src/xfdesktop4/4.20.2-2/debian/rules/)
selects that path. The builder now supplies the missing link to the native
`desktop-background` alternative only for `devuan-ceres` with XFCE. Existing
valid defaults and user wallpaper settings are preserved. The failure and repair
were reproduced against files extracted from the affected ISO, with the native
image hash unchanged; 586 automated tests passed. The subsequent Ceres VM retest
confirms that the corrected wallpaper loads properly.

The installed Ceres GRUB menu used a generic appearance and the `GNU/Linux`
label; the user reports the same issue on Freia. Native Calamares `grubcfg`
replaces `/etc/default/grub`, dropping the theme unless explicitly included in
its defaults, and sets the distributor from branding `bootloaderEntryName`
rather than the configured `GRUB_DISTRIBUTOR` default. The builder now supplies
both values. Running the actual Calamares 3.4.3 module extracted from the Ceres
ISO against temporary plain and encrypted target fixtures confirmed the theme,
product label and encrypted-root arguments. The complete suite passes 590 tests.
The user subsequently confirmed the corrected GRUB behavior on Ceres; the
equivalent Freia retest remains pending.

GParted also displays `Not automatically fixing this.` in the EFI label column.
This text is a [dosfstools diagnostic](https://github.com/dosfstools/dosfstools/blob/master/src/boot.c)
for a difference between the FAT boot sector and its backup, rather than proof
of that literal on-disk volume label. The Ceres VM's `blkid` output has no volume
label; `fatlabel` reports only `65:01/00`. Offset 65 is FAT32's mount-state byte,
and bit 0 is `FAT_STATE_DIRTY`. The [Linux FAT driver](https://github.com/torvalds/linux/blob/master/fs/fat/inode.c)
sets it on mount and clears it on unmount, updating the primary boot sector.
This is consistent with the mounted ESP shown in the screenshots, rather than
evidence requiring a repair. No automatic repair has been added.

The user reports the missing APT indexes only on Devuan; the installed Ceres
system needed `apt update` before finding GParted. A bundled
cleanup hook deleted APT indexes regardless of `cleanup.apt_lists: false`.
Index cleanup is now left to `ChrootCleaner`, which honors that setting; the
20 cleaner and hook tests pass.

A fresh real amd64 Ceres XFCE/Calamares build completed with all three changes:
`deb-dev-devuan-ceres-live-iso-amd64-calamares-xfce-live-sysvinit-generic-grub2-hybrid-ext4-real-20261110T122542Z-7ba26e18.iso`
(1,588,140,032 bytes). Its SHA-256 matches the generated checksum. Inspection of
the final ISO's SquashFS confirms the GRUB theme configuration and branding,
the XFCE fallback link, the unchanged native wallpaper hash and four APT package
indexes. The user subsequently confirmed successful encrypted Calamares
installation and boot on Ceres, with a single passphrase prompt, working GRUB
and LightDM, and the wallpaper loading correctly. The APT screenshot shows
`apt install gparted` resolving the package and dependencies directly; a later
GParted screenshot confirms encrypted Btrfs mounted at `/` and `/home`, plus
encrypted swap. The user reports no remaining functional complaint in this VM
test. This validates the tested Ceres XFCE configuration, without extending that
runtime result to untested profiles. The shared GRUB and cleanup fixes also
apply to subsequent Freia and Debian builds.

## Distribution coverage

Use the existing release profiles as separate validation targets:

| Family | Oldstable | Stable | Testing | Unstable |
|---|---|---|---|---|
| Debian | `debian-12` / Bookworm | `debian-13` / Trixie | `debian-14` / Forky or `debian-testing` | `debian-sid` |
| Devuan | `devuan-5` / Daedalus | `devuan-6` / Excalibur | `devuan-testing` / Freia | `devuan-ceres` |

Codename profiles and the moving `testing` alias are both useful, but their
results must identify the actual suite, package versions and artifact. Testing
and unstable can change between builds; success on stable does not validate
them automatically. Unsupported desktop/suite combinations remain exclusions,
not failed runtime tests of an allegedly supported build.

## Configuration checks

```bash
python3 tools/audit-desktops.py --distro debian-13 --distro devuan-6
python3 tools/audit-desktops.py --distro debian-13 --installer debian-installer
python3 tools/audit-desktops.py --all-distros
```

These commands compose the real build profiles and run the builder's validation
without building an ISO, installing packages or touching VM settings. JSON
results explicitly distinguish configuration validity from runtime testing.
Select `--desktop kde` to narrow a report to the next desktop under test.

Live login and d-i autologin use the session candidates in
`configs/desktops/session-defaults.json`, including `plasma` and `plasmax11`.
An explicit `desktop_session` takes precedence. SDDM receives the full session
filename, including `.desktop`. The PipeWire profiles include `pactl` and
`paplay` through `pulseaudio-utils`; this does not select the PulseAudio daemon.

## Test each live and installed desktop

1. Boot with UEFI, enter the intended desktop and check logout/login, screen
   locking, terminal, file manager, keyboard layout and display resolution.
   Check the actual session type when a desktop offers both X11 and Wayland.
2. Check wired and wireless networking where available, DNS, the browser,
   PolicyKit authentication, removable-media mounting, and the absence of
   competing authentication agents. Test Bluetooth and printing with hardware
   when available; a VM without those devices cannot validate them.
3. Test an audible tone and an actual music/video file. Check input recording
   separately if a microphone is available. Useful diagnostics are `wpctl
   status`, `pactl info`, `pactl list short sink-inputs`, and `aplay -l`.
4. Open the selected installer. Verify language, timezone, keyboard, account
   creation, installation, bootloader installation and first boot. Test the
   requested autologin choice, logout, and authentication after installation.
5. Test plain and encrypted layouts separately; include LVM, BIOS, other
   filesystems and architectures as additional combinations, not implied passes.
   Confirm the encryption prompt, installed initramfs and successful boot.
6. Confirm installed repositories, updates, locale, desktop settings, audio,
   network and the removal of live-only installer launchers. Reboot and shut
   down; in live mode, verify the medium-removal message and Enter handling.

Record the exact artifact filename, architecture, distribution, init system,
installer, layout, session type and VM hardware with each result. Mark a
combination working only after its actual tests pass.

## Functional baseline audit, 2026-11-09

The original nine desktop and window-manager profiles were reviewed without adding visual
customization. The common baseline now explicitly supplies networking and Wi-Fi,
audio, D-Bus, PolicyKit, removable-media support, power-management interfaces,
user directories and desktop portals. Session PAM packages match systemd or
elogind. Service detection uses the selected init system's native service files.

Profile-specific additions cover authentication agents, terminal/file-manager
commands used by native defaults, screen locking and power management. Package
aliases account for renamed or removed packages across supported suites.
The i3, Sway and Hyprland session integration starts the required authentication
and XDG autostart processes. Wayland sessions receive a D-Bus session and the
installed keyboard layout. Native appearance and key bindings are retained.
Hyprland's installed upstream example is copied before appending startup lines.

SDDM's `DisplayServer` controls the greeter independently of the desktop session.
The greeter uses X11; Sway and Hyprland still start native Wayland sessions.
Stable Hyprland profiles enable the matching Debian or Devuan backports archive
and select that release for the package-install command so dependencies resolve.
This does not set a persistent global APT default release.

Official amd64 package-index snapshots were checked for all declared packages.
Native `apt-get -s` simulations used isolated indexes, an empty dpkg status and
the profiles' exclusions, with recommendations enabled. Of 70 distinct
suite/desktop combinations initially accepted, 68 resolved successfully.
The two failures were GNOME on Devuan Freia and Ceres: GDM requires
`libsystemd0 (>= 259)`, while the available `libpam-elogind` requires
`libelogind-compat`, which conflicts with `libsystemd0`. Those combinations now
fail configuration validation until repository compatibility is restored.
Devuan Excalibur GNOME remains available. Hyprland on Bookworm and Daedalus
remains unsupported. Including Debian's testing alias, 77 of 81 configurations
are therefore accepted; all accepted combinations had their package names and
dependencies checked against these snapshots.

To check downloaded indexes without network access or package installation:

```bash
python3 tools/audit-desktops.py --distro debian-13 \
  --package-index debian-13=/path/to/main/Packages.xz \
  --package-index debian-13=/path/to/non-free/Packages.xz \
  --package-index debian-13=/path/to/non-free-firmware/Packages.xz \
  --package-index debian-13=/path/to/backports/Packages.xz
```

This command checks declared names and virtual providers, not dependency
resolution. The APT simulations described above are separate verification.
Neither establishes a successful ISO boot or installation, and this audit did
not validate other architectures. Fresh VM tests remain necessary for MATE,
LXQt, i3, Sway and Hyprland, including native sessions, authentication, locking,
portals and suspend/resume. OpenRC and runit need separate boot tests.

The subsequent [catalog expansion](desktop-catalog.md) adds further native
desktops and window managers. Its new profiles require fresh graphical boot and
installation tests; the runtime evidence above continues to identify only the
sessions actually reported by the user. The 81-combination audit above describes
the original nine profiles, not the expanded catalog.

References: [SDDM configuration](https://github.com/sddm/sddm/blob/v0.21.0/data/man/sddm.conf.rst.in),
[Debian Hyprland versions](https://sources.debian.org/src/hyprland/),
[Debian Sway configuration](https://wiki.debian.org/sway).
