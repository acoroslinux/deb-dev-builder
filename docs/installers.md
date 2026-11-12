# Installers

The detailed research notes for future d-i overlays, initrd repacking, scripts
and GTK customization are in [the live-build reference guide](live-build-reference.md).
Implemented initrd editing, shared live-package cleanup and JSON options are
documented in [the applied improvements guide](live-build-applied.md).
The [installation-flow audit](installer-flow-audit.md) documents lifecycle
validation, identity handling and the optional d-i autologin question.

For desktop d-i installations, automatic login can be selected near the end of
installation or preseeded with `d-i deb-dev/autologin boolean true`. It defaults
to false. Policy and LightDM/SDDM/GDM mappings live in
`configs/debian-installer/autologin.json`; account passwords, disk encryption
and sudo policy are unchanged by this choice.

## Calamares live installer

Calamares optionally suggests the interface language from the country reported
by KDE's GeoIP service and the timezone from its timezone endpoint. These
lookups require internet access but do not make it an installation requirement.
Without a response, the installer keeps its language and uses the live system's
timezone (UTC is the configured fallback). Language, regional formats and
timezone can still be reviewed and changed before installation. Shared module
configuration applies to both Debian and Devuan and requires no systemd service.

Calamares is available only on a live ISO with a desktop. The builder installs
the `configs/software/calamares.json` package profile, deploys the custom
Calamares settings, creates the desktop launcher and adds the live-session
polkit rule.

```bash
sudo python cli.py amd64 --distro debian-13 --desktop kde \
  --with-calamares --format iso --mode real
```

Validation rejects Calamares without a desktop, on VM/container output, or in
installer-only modes.

The Calamares software profile explicitly includes `cryptsetup`,
`cryptsetup-bin` and `cryptsetup-initramfs` for encrypted installations. Both
init-system sequences configure and rebuild the installed initramfs after
writing crypttab and removing live packages, before generating GRUB settings.

The `grubcfg` job writes `/etc/default/grub` directly (`prefer_grub_d: false`).
Its native partition detection enables `GRUB_ENABLE_CRYPTODISK=y` when GRUB must
read encrypted boot files, while leaving it unset for an unencrypted separate
`/boot`. The alternate Calamares output `grub.d/00calamares` is unsuitable here:
`grub-install` needs the main defaults file and Debian's drop-in loader expects
a `.cfg` suffix.

Installer storage support is shared by Calamares and d-i live through
`configs/storage/installer.json`: cryptsetup, its initramfs integration and
LVM are installed in the target package pass, rather than being live-only
dependencies. `systemd-cryptsetup` is additionally selected for the configured
systemd suites that ship this split package; Bookworm does not require it.

## Debian Installer

The three modes use the selected distribution's official d-i kernels and
initrds for the suite and dpkg architecture. Debian profiles use Debian images;
Devuan profiles use Devuan's native installer. They never reuse the live kernel.
The existing `--with-debian-installer` flag selects either distribution's d-i.

Before packing a d-i live filesystem, the builder materializes hardlinked regular
files under `/boot`. This preserves their contents and metadata while allowing
installation onto a separate `/boot` filesystem: BusyBox tar cannot restore
hardlinks between `/boot` and `/usr/lib/modules` across filesystems. The installer
also verifies the copied usr-merge layout and executable loader before native
configuration hooks, and requires a successful check before finishing.

| Mode | Installer source | Installation behavior |
|---|---|---|
| `live` | `images/cdrom` | Copies `/live/filesystem.squashfs`, preserving the built desktop, packages and customizations |
| `netinstall` | `images/netboot` | Minimal bootable ISO; installs the system from network repositories |
| `netboot` | Official `images/netboot/netboot.tar.gz` | PXE/TFTP boot tree; installs through the network |

Debian testing/Forky and Sid use the official daily d-i images from
`https://d-i.debian.org/daily-images/{architecture}/daily`, paired with installer
components from Sid. The installed system still uses the selected target suite.
The builder requires an exact match between the installer kernel and module
udebs. Live ISO builds resolve and freeze those inputs before toolchain setup,
rootfs bootstrap or SquashFS compression; later build stages use that same set.
Stable Debian and Devuan keep their suite-specific installer images. Explicit
installer mirror overrides select the archive image layout instead of daily images.

Installer-only netinstall and netboot builds skip rootfs bootstrap and package
installation. A netinstall ISO here is a minimal network installer, without a
local base-package repository; it requires a working network connection.

Mode sources, download policy, live preseeding, component exclusions and menu
labels are configured in `configs/debian-installer/media.json`.

### Coherent installer bundles during archive updates

The `bundle` policy in `configs/debian-installer/media.json` applies to live
Debian and Devuan installer media. Each bundle contains the original text and
available graphical images, the udeb index and every selected udeb. Image hashes
come from the native `SHA256SUMS`; package hashes come from the selected index.
Kernel ABI must match. Embedded component versions are recorded for diagnostics;
normal native userspace updates do not require identical versions in the image
and archive. The target live kernel may differ from the installer kernel.

The resolver first tries the configured native archive. If Debian's daily images
lag behind Sid, it tries timestamped
[Debian Snapshot](https://snapshot.debian.org/) repositories around the images'
publication time. These repositories supply installer components only; the
installed system retains its requested distribution and normal APT sources.
Download and generation retries are bounded and configurable.

Only a completely downloaded and verified bundle becomes `last-good.json`.
Bundles are stored under
`cache/installer-bundles/{family}/{suite}/{architecture}/{source}` and rechecked
before use. An unchanged image checksum list reuses the frozen set without
reading a newer package index. If refreshing fails, the last verified complete
bundle is retained. Build reports record its identifier, kernel, repository and
manifest/index hashes. Initrd customization modifies staged copies only.

Devuan uses the same cache and fallback mechanism with its own native images,
merged archive and required `choose-init` component. It never falls back to
Debian images, repositories or bundles. No Devuan snapshot endpoint is configured
by default; `bundle.snapshot_mirrors.devuan` can supply a native HTTPS archive
with a `{timestamp}` placeholder when one is available. A first build still needs
a coherent upstream set: without a compatible archive/snapshot or a previously
verified bundle, it stops during preflight rather than producing broken media.
Netinstall and netboot remain network installer modes outside this live bundle
policy.

### Devuan installer origins and init selection

The `distributions` map supplies family defaults before explicit runtime
overrides. For Devuan, images come from
`https://pkgmaster.devuan.org/devuan/dists/{suite}/main/installer-{architecture}/current/images/`;
udeb indexes and their `pool/DEVUAN` / `pool/DEBIAN` payloads come from
`https://pkgmaster.devuan.org/merged`. This follows the
[official Devuan download guide](https://www.devuan.org/get-devuan).
Do not derive image URLs from the merged APT mirror. Image and archive origins
can be overridden separately with `runtime.installer.image_mirror` and
`runtime.installer.archive_mirror`, or the direct configuration keys
`installer_image_mirror` and `installer_mirror` respectively.

Devuan uses its own udeb cache namespace and `Origin` / `Label` metadata. Live
media include the native `choose-init` module and show its SysVinit, OpenRC or
runit selection dialog. The build's selected init is the initial suggestion;
the question is marked unseen so that the installer asks for confirmation.
All three init package sets and their dependency closures are downloaded
separately for offline installation, since the alternatives can conflict.
Set `runtime.installer.init_ask: false` for an unattended install that retains
the build's selected init and downloads only that init's package set.
Kernel ABI matching and SHA256 validation of udebs
remain mandatory, including packages inherited from Debian through the merged
archive. This does not replace Devuan's forked installer components with their
Debian counterparts.

Devuan's native `choose-init` calls target APT directly rather than using the
installer's `apt-install` CD-handling helper. For live installs, the builder
therefore bind-mounts the current `/cdrom` at `/target/media/deb-dev-installer`.
A temporary target APT configuration selects this already available medium,
disables CD autodetection and prevents independent remount/eject attempts.
The normal CD identity database and package hash checks remain in use. The
binding is shared across target package operations, including init selection;
it is not a replacement for the native `choose-init` module.

The media binding must be ready before final boot validation. Its finish hook
runs after that validation and before native `15cdrom-detect`, releasing the
binding and restoring/removing the temporary APT configuration before the
normal installation-media eject step. The policy is under `target_cdrom` in
`media.json`; it is enabled by default only for Devuan. The existing Debian
flow is unchanged. Tests cover preparation/cleanup and a native APT CD-ROM
fetch with media identification and a SHA256 check; the rebuilt Devuan ISO
still requires a complete VM installation test.

Automatically selected network-install profiles receive Devuan mirror/suite
answers and the selected init answer. Explicit `--preseed` files and named
profiles remain authoritative: their mirrors, suites and package selections
must be suitable for Devuan. The shared GTK overlay and mandatory installation
hooks are applied after staging the native initrds.

```bash
sudo python cli.py amd64 --distro devuan-6 --desktop xfce --init-system sysvinit \
  --with-debian-installer --di-mode live --mode real
```

Installer publication is suite-dependent. An image directory alone does not
establish compatibility with the available module udebs. Missing or incompatible
inputs fail preflight unless a complete verified native bundle is already
cached; the builder does not substitute Debian or another Devuan suite.
Configuration support is not a claim that every suite/architecture combination
has compatible published media. Native source selection, repository composition,
init answers and hook preservation are tested; these checks do not replace a
complete VM installation and installed-disk boot.

### Known upstream limitation: Devuan Ceres / unstable

Verified on **2026-11-12**, for **amd64**: no compatible native d-i image/module
set is available from the checked official Ceres sources. Ceres is Devuan's
unstable branch; this limitation concerns Devuan, not Debian Sid.

| Input | Verified state |
|---|---|
| [Dated installer images](https://pkgmaster.devuan.org/devuan/dists/ceres/main/installer-amd64/20260628devuan1/images/) | Build `20260628devuan1`; kernel `7.0.13+deb14-amd64`, recorded in `MANIFEST.udebs` |
| [Current image checksums](https://pkgmaster.devuan.org/devuan/dists/ceres/main/installer-amd64/current/images/SHA256SUMS) | Identical to the dated build's `SHA256SUMS`; using the dated URL supplies the same images |
| [Merged Ceres installer index](https://pkgmaster.devuan.org/merged/dists/ceres/main/debian-installer/binary-amd64/Packages.gz) | Module udebs only for `7.3.4+deb14-amd64`; no matching `7.0.13+deb14-amd64` modules |

The presence of `live-installer` and `choose-init` does not resolve this kernel
ABI mismatch. A new Ceres live ISO with d-i cannot obtain its first complete
bundle from these sources. Record this combination as **blocked by upstream
installer availability**, not as a successfully validated installation path.
The cache mechanism can preserve a compatible set once one becomes available;
it cannot supply missing historical modules on its first use.

Recheck this status when Devuan publishes updated native installer images or a
matching native archive becomes available. Keep the compatibility check dynamic
so a future coherent publication can work without removing a permanent block.
Freia/Testing is a separate suite: its amd64 installer image directory returned
HTTP 404 when checked on 2026-11-08; it has not been revalidated by this Ceres check.

### Appearance and advanced startup options

The graphical installer uses the compact DebDevModern GTK2 theme, with a navy
header, blue accents, visible keyboard focus and matching installer artwork.
The banner fits 640-pixel-wide displays and leaves room on the right for native
installer status text. The theme uses the Clearlooks engine already supplied
by d-i; it does not require desktop packages or systemd. Native translations,
language-specific fonts, keyboard navigation and zoom remain available.
The GTK overlay includes the regular DejaVu Sans face alongside the bold and
oblique faces supplied by the native udeb. This prevents body text and lists
from falling back to bold; native title emphasis is preserved.
The original text frontend and native high-contrast theme are preserved.

GRUB and ISOLINUX offer additional native modes, configured through
`extra_boot_entries` in `media.json`:

- **Expert install (text)** exposes low-priority installation questions.
- **Rescue an installed system (text)** starts d-i rescue mode.
- **Accessible install (graphical, high contrast)** selects the original
  `theme=dark` accessibility theme. This entry is omitted when no graphical
  installer image is available.

The GTK-only overlay lives in `configs/debian-installer/includes/gtk`; disable
optional customization with `runtime.installer.customization.enabled: false`.
It is embedded into graphical live-install, network-install and netboot initrds
through the same validated repacking path. Required installation hooks keep
their precedence over optional artwork. The editable banner source is
`configs/debian-installer/artwork/banner.svg`. Regenerate the two PNG banners
with `rsvg-convert` after editing it; the installer itself needs only the PNGs.

For a disposable, representative GTK2 widget preview (not a full installation):

```bash
xvfb-run -a env GTK_IM_MODULE=gtk-im-context-simple NO_AT_BRIDGE=1 \
  python3 tools/preview-di.py --size 640x480 --output /tmp/di-preview.png
```

The preview requires host GTK2 and the Clearlooks engine. `--gtk-path` can point
to a GTK2 engine tree extracted from the matching `gtk2-engines-udeb`. Theme
rendering, frontend isolation and boot-menu generation are checked locally;
complete expert/rescue flows and the final appearance still require VM testing
with a rebuilt ISO.

Live ISO with d-i installing the built system:

```bash
sudo python cli.py amd64 --distro debian-13 --desktop xfce \
  --with-debian-installer --di-mode live --mode real
```

The live mode supplies a SHA256-verified local udeb repository, including
`live-installer`, and a mandatory offline bootloader/storage repository. Its explicit
architecture-specific package sets include the GRUB EFI/BIOS metapackages and
their full dependency closures, even when those packages are already installed
in the live root. Conflicting EFI and BIOS sets are resolved separately using an
empty temporary APT status database; nothing is installed into the live system
by this download step. This repository is automatic and does not require
`--with-offline-repo`; that option still controls extra optional packages.
Both text and
graphical initrds embed the live preseed, so the manual menu entries also select
SquashFS installation. The native `live-installer/mode` answer is `normal`:
this installs a normal disk system rather than retaining live boot semantics.
Desktop network-install preseed profiles are not selected automatically for
live media. Live policy disables tasksel/package upgrades and network mirrors
by default, enables the installation CD as an APT source, and avoids asking for
another CD; explicitly selected preseed files still supply other answers.
The ISO build fails if required bootloader packages are absent from its local
repository. Package sets and CD preseeding are owned by `media.json`.
The storage package set and its dependency closure are downloaded separately
as well, even if already installed in the live filesystem. Native partman
attempts to install cryptsetup/LVM but tolerates package-installation failures;
their presence in the installer environment does not supply the target OS.
The native Debian base-installer sets CD trust through its own `00trustcdrom`.
The builder does not duplicate that authentication setting. A temporary APT
file, sorted after the live system's `99optimize`, disables recommendations
during offline installation and applies `--force-confdef`/`--force-confold` to
resolve dpkg conffile questions while preserving existing customizations. The
finish hook removes this temporary policy. The prepared `.deb`
files are indexed and checked before ISO creation.

GRUB manages `/etc/default/grub` through `ucf`, separately from dpkg conffile
handling. Live d-i installs temporarily add a destination-specific rule to the
target's `/etc/ucf.conf`, retaining the copied `/etc/default/grub` without a
maintainer-version question. Other destinations keep their existing ucf policy.
The original ucf configuration is backed up with its permissions and restored
at completion; if it was originally absent, the temporary file is removed.
This leaves normal ucf behavior available for future upgrades. Preparation and
restoration are idempotent, and the final target validation requires evidence
that the preservation policy was prepared. Paths and protected destinations
are configured in `media.json` under `ucf_preservation`. Native GRUB package
configuration, bootloader installation and final menu regeneration still run.
The behavior follows [ucf's preservation controls](https://manpages.debian.org/trixie/ucf/ucf.1.en.html)
and is tested against the native script with isolated state and debconf transport.

The live source `file:/run/live/medium/repo/` is removed from the copied target:
that mount path exists only in the running live session. A dedicated hook also
temporarily saves inherited `sources.list`, `.list` and deb822 `.sources` files
outside `sources.list.d`, before native d-i configures its CD source. The finish
hook restores normal sources and leaves the live-only source removed. Paths and
the live source template belong to `customization/offline-repository.json`.
APT preparation runs separately from live-account cleanup so it cannot be
skipped by an account-removal failure. Calamares target cleanup also removes
the live-only APT source.

The archive layout follows live-build's `installer_debian-installer` and
`binary_disk` scripts (reference reviewed at commit `531cdb985`): `.deb` files
use `pool/<component>/<prefix>/<source>/`, udebs use `pool-udeb/`, and indexes
remain separate. Each binary component gets the mirror's `Release`; the
top-level `Release` is generated by `apt-ftparchive release`, with its checksum
sections. Live media declares `.disk/base_components`, `.disk/cd_type=live`,
and the conventional `debian -> .` symlink. These values belong to `media.json`.
The optional flat `/repo` and target-source preservation hooks remain specific
to this builder; this is not a full live-build backend.

An installer hook removes the temporary live account, its sudo rule, generated
live autologin settings and installer launchers before d-i configures the final
user. The standard live-installer finish step removes live boot packages.
After that purge and autoremove, a generated finish hook reinstates and marks
the storage packages as manual, sets the configured cryptsetup initramfs option,
and rebuilds every installed initramfs. It checks for cryptroot, LVM binaries
and udev activation rules, and the encrypted-volume configuration when crypttab
has entries. Missing support fails the hook instead of silently accepting an
unbootable target. Failures return native finish-install status 10 to stop and
return to the installer menu, since ordinary nonzero exits are only logged by
that runner. This runs before restoring the final APT sources, while
the offline CD is still usable. Modern Debian LVM activation uses the
[packaged udev rules](https://packages.debian.org/trixie/amd64/lvm2/filelist),
not a presumed `scripts/local-top/lvm2` file.
Configured aliases also accept the older `/sbin` and `/lib/udev` archive paths.
The network-install overlay late-command is not run on the copied live system.

Installer-only netinstall ISO:

```bash
sudo python cli.py amd64 --distro debian-13 \
  --di-mode netinstall --preseed server --format iso --mode real
```

PXE/TFTP netboot archive:

```bash
sudo python cli.py amd64 --distro debian-13 \
  --di-mode netboot --preseed server --mode real
```

The netboot result is `.netboot.tar.gz` and contains the complete official d-i
netboot tree plus the selected `preseed.cfg`, `early_command.sh`, and
`late_command.sh` when present. Named preseed files are resolved below
`configs/debian-installer/`; an explicit filesystem path is also accepted.
Missing explicit preseed files are validation errors.

For PXE, serve the extracted archive through TFTP and configure the network's
DHCP boot settings. Optional preseed files in the archive must be exposed at
the URL used by the PXE kernel command line.

## Calamares completion and restart

The final page selects **Restart now** by default. Clicking the final **Done**
button after successful installation restarts the live system; the user can
uncheck the option to remain in the live session. This does not start a timed
restart before the final confirmation.

The systemd variant uses `systemctl -i reboot`. The non-systemd variant uses
`/sbin/shutdown -r now`, supplied by the selected init system. Their native
`finished.conf` files are kept with the corresponding module overlays.
Calamares disables restart after an installation failure. The existing
live-medium-removal prompt remains part of the normal shutdown flow.
