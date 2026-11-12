# live-build reference and future Debian Installer customization

Status descriptions record the time of the investigation. See
[implemented improvements](live-build-applied.md) for subsequent implementation.
"Observed" describes inspected code/artifacts; "Proposed" does not mean a
feature is already available in our CLI. The wider package/cache/firmware/boot
survey is in [additional mechanisms](live-build-project-reference.md).

## Examined references

- Local clone: `/iso-builder/live-build`.
- Commit: `531cdb9854e04d08c3c3770463454d8a5e91b129`, dated 2026-09-13.
- Main sources: `scripts/build/installer`, `installer_chroot`,
  `installer_debian-installer`, `installer_preseed`, `binary_includes`,
  `binary_disk`, `binary_bootloader_splash`, `binary_grub_cfg`, `binary_syslinux`.
- Templates: `share/bootloaders/grub-pc/install_gui.cfg`, `install_text.cfg`,
  and `data/debian-cd/trixie/`.
- Clone documentation: `manpages/en/lb_config.1`.
- GTK resources inspected in a trixie/amd64 d-i initrd: `cdebconf-gtk-udeb`
  0.280, `rootskel` 1.138, `rootskel-gtk` 13.0.4. Paths below were confirmed
  in that artifact and must be inspected again for another version.

The clone is a development reference, not a build/runtime dependency.

## Observed: stages and isolation

`scripts/build/installer` prepares a separate installer-building environment,
runs `installer_debian-installer` and `installer_preseed`, then reverses that
preparation. `installer_chroot` uses the bootstrap cache as a tool environment,
keeping the live rootfs in `chroot/chroot`.

Keep toolchain, live system and published ISO trees distinct. Do not copy
upstream directory changes literally: our corresponding concepts are
`build_host`, `target_root` and `iso_root`. Neither upstream stages nor hooks
executed by d-i change our three public stages: `pre-chroot`, `chroot`,
`post-chroot`.

## Observed: four different file destinations

| Reference destination | Runtime location | Purpose |
|---|---|---|
| `config/includes.chroot*` | Live system/SquashFS | Desktop/system configuration potentially copied to disk |
| `config/includes.binary` | Installation-medium root, normally `/cdrom` in d-i | Helpers, documentation and resources available after mounting media |
| `config/includes.installer` | d-i initramfs root | Early scripts/resources, themes and installer modifications |
| `/target` changes or `in-target` commands | Installed system | Final configuration, services and cleanup |

A tree on the ISO is not automatically in the initrd. A live-system path such
as `/run/live/medium` is not automatically valid inside `/target`.

## Observed: obtaining and building d-i

`installer_debian-installer` selects its source by mode:

| Upstream mode | d-i image type |
|---|---|
| `live`, `cdrom` | `images/cdrom`, except explicitly handled variants |
| `netinst`, `netboot`, `businesscard` | `images/netboot` |

Our `netinstall` corresponds to upstream `netinst`. Text and GTK have separate
kernels/initrds; the live kernel cannot substitute for the d-i kernel.

Upstream also supports daily images and rebuilding from Git. Its Git path
installs dependencies in a chroot, chooses architecture targets, uses
`localudebs`, computes kernel ABI and builds debian-installer. That capability
did not exist in our builder at the time of this survey.

Potential follow-up work:

- Record original artifact identities/checksums and key caches by mirror,
  suite, architecture, frontend and version.
- Verify kernel/initrd/module-udeb agreement.
- Treat GTK availability as an architecture capability with explicit errors.
- Separate a future Git-rebuild backend from the official-image backend.
  New pages or compiled behavior may require component rebuilds, not overlays.

## Observed: unpacking, overlaying and repacking

`Repack_initrd`, near the end of `installer_debian-installer`:

1. Creates a work directory and resolves the overlay's absolute path.
2. Decompresses gzip and extracts CPIO using
   `cpio -i --make-directories --no-absolute-filenames`.
3. Applies `config/includes.installer` through CPIO with NUL-terminated paths,
   file updates and mode preservation, without preserving overlay ownership.
4. Creates a new `newc` archive with `find -print0` and `cpio -H newc -o0`.
5. Compresses with `gzip -9` and replaces the destination initrd.
6. Removes the temporary directory.

This applies to `install/initrd.gz` and, when present, `install/gtk/initrd.gz`.
A nonempty overlay is applied independently of preseed inclusion.

This was a reference algorithm, not yet an implemented editing tool here.
An implementation must preserve the original, validate paths/links before
extraction, use an exclusive work directory, preserve required metadata and
publish only after verification. Give scripts appropriate modes rather than
applying a tree-wide `chmod`. Keep temporary output on the destination
filesystem for atomic replacement.

### The initrd behavior at the time of investigation

`LiveInstallerMedia.embed_preseed()` then appended another gzip member holding
CPIO `newc` with preseed/generated hooks; it did not fully unpack/repack or
provide arbitrary removals/patches.

A local experiment concatenated two `preseed_archive()` results.
`gzip -dc | cpio -i --to-stdout preseed.cfg` returned only the first file.
Naive extraction could therefore lose appended hooks during repacking.

The preferred future design starts from a verified original, applies declared
overlays and regenerates required hooks in a defined order. Accepting already
modified initrds requires correctly merging all members/archives in order,
rather than assuming a single CPIO. See the implementation guide for the
subsequently implemented editor.

## Observed: preseed, scripts and execution order

`installer_preseed` concatenates `config/preseed/*.cfg`, `*.cfg.installer` and
optional `packages.installer` into `binary/install/preseed.cfg`. Certain network
images also embed preseeds during repacking. These are concatenated debconf
answers, not semantic JSON merging.

The inspected startup includes `S30initrd-preseed`, `S32env-preseed` and
`S33preseed_early_command`. Preseed location determines answer/early-command
availability.

| Mechanism | Context and main consideration |
|---|---|
| Initrd `/preseed.cfg` | Available before media discovery |
| `/cdrom/install/…` preseed | Needs mounted media and explicit selection |
| `preseed/early_command` | Installer environment; `/cdrom` and `/target` may not exist yet |
| `preseed/late_command` | Still installer context; use `in-target` for target commands |
| `debian-installer-startup.d` scripts | Examined launcher executes executables and sources other files |
| `debian-installer.d/SNN…` scripts | Examined launcher sources files; they can configure frontend environment |
| `post-base-installer.d` | Check caller and available mounts at each lifecycle point |
| `finish-install.d` | Numbering and `/target` unmount timing matter |

Do not assume all d-i hooks execute identically. Check that version's launcher;
environment changes persist only when files are sourced. Features introducing
dependencies, dialogs or custom steps may deserve a `.udeb` with debconf
templates and maintainer scripts. Frontend changes may require rebuilt udebs
and matching libraries.

## Observed: GTK installer appearance

The examined trixie/amd64 initrd uses GTK 2. Desktop GTK themes are not
automatically applied to d-i.

| Confirmed resource | Observed role |
|---|---|
| `etc/gtk-2.0/gtkrc` | Theme/font, here Clearlooks and DejaVu Sans 9 |
| `usr/share/themes/Clearlooks/gtk-2.0/gtkrc` | Available graphical theme |
| `usr/share/themes/dark/gtk-2.0/gtkrc` | Available dark variant |
| `usr/share/graphics/logo_debian.png`, `logo_debian_dark.png` | Distribution images |
| `usr/share/graphics/logo_installer.png`, `logo_installer_dark.png` | Frontend names, here symlinks to Debian logos |
| `usr/share/graphics/logo_installer.ini` | Symlink to banner configuration `logo_debian.ini` |
| `usr/share/graphics/note_icon.png`, `warning_icon.png` | Frontend icons |
| `usr/lib/debian-installer.d/S65theme` | Pre-menu theme selection |
| `usr/bin/gtk-set-theme` | Updates `gtk-theme-name` in `gtkrc` |

`logo_debian.ini` has a `[banner]` section specifying text placement and
expansion direction. Frontend strings confirm use of `logo_installer*`;
`logo_debian*` files alone do not establish resource wiring. Preserve or replace
links deliberately.

Upstream templates offer `theme=dark`, normal, expert, automatic and rescue
installation. `S65theme` reads `debian-installer/theme`, with different GTK and
text/newt mechanisms. Dark mode also applies accessibility answers; retain
those features when changing appearance.

Start future customization with compatible banners/colors, preserving fonts,
accessibility, terminal, errors and text fallback. GTK 3/4 CSS cannot directly
replace GTK 2 `gtkrc`; changing toolkits is frontend/packaging work.

## Observed: boot menu and d-i are separate layers

`binary_bootloader_splash` handles GRUB/syslinux artwork, SVG/PNG, version/date/
architecture substitutions and bootloader-specific dimensions. That does not
establish GTK frontend banner customization.

`install_gui.cfg` and `install_text.cfg` connect kernels/initrds to normal,
expert, automatic, rescue and dark modes. Test both frontends and BIOS/UEFI
when changing menus; do not accidentally couple GRUB and GTK themes.

## Observed: udebs, repository and kernel

- Support packages are downloaded with an empty APT status database; potentially
  conflicting packages are resolved separately.
- Pools are arranged by component/source with separate `.deb` and `.udeb`
  indexes. Component binary `Release` is imported and the overall `Release`
  generated with `apt-ftparchive`; suite aliases and `debian -> .` are published.
- `data/debian-cd/<suite>` controls udeb inclusion/exclusion. Its README describes
  debian-cd copies with a local `apt-mirror-setup` modification.
- Live mode adds `live-installer` to the inclusion list.
- amd64 identifies the d-i kernel ABI to exclude other module versions; this
  filter was not yet implemented here at the time of the investigation.
- Git rebuilds accept local udebs. Component changes must preserve dependencies,
  templates, maintainer scripts and indexes.
- Upstream also prepares firmware discovery data. Live firmware presence alone
  does not reproduce the entire d-i mechanism.

Upstream exposes the pool to live sessions under the media-mount URI and
copies APT indexes into rootfs, avoiding an initial update. Our pipeline did
not reproduce that full preload: it built SquashFS before assembling the d-i
repository in the ISO tree.

## Builder status recorded during investigation

| Capability | Status at that time |
|---|---|
| Official text/GTK sources and live/netinstall/netboot modes | Implemented, not every upstream variant |
| Local boot repository and ISO metadata | Implemented as described in `installers.md` |
| Embedded live-initrd preseed/hooks | Implemented by archive append |
| Generic initrd overlays, removals and patches | Not yet implemented |
| Full frontend repacking | Not yet implemented |
| Local udebs and d-i rebuilds | Not yet implemented |
| Profile-driven GTK themes/logos | No generic interface yet |
| Netboot customization | Auxiliary files present; no generic initrd/PXE editor yet |

Issues requiring attention before further customization:

1. Generated `early_command.sh` was referenced through `/cdrom`, which might
   not be mounted when an embedded preseed runs it.
2. Legacy `late_command.sh` used fixed accounts and implied target execution
   although called from the installer. It was excluded from live mode and
   needed review before reuse.
3. `SystemCustomizer` interprets `custom_files` groups; raw copying into
   `/target` does not apply that destination mapping.
4. ISO `splash.png` plus a preseed key is not proof that a frontend consumes it.
   Observed banners live inside the initrd at the paths above.
5. Existing resources can be symlinks. Overlay policy must distinguish replacing
   a link from replacing its target.
6. Builder hooks and user-machine installer hooks have separate lifecycles.

These were recorded gaps, not features implemented by the survey itself.

## Proposed declarative editor interface

Use a separate initrd editor rather than embedding editing logic in ISO
assembly. JSON selects resources/order; GTK, preseed and shell retain native
consumer formats. This conceptual example was **not accepted by the CLI**:

```json
{
  "enabled": false,
  "frontends": ["text", "gtk"],
  "base": "verified-original",
  "overlays": [
    {"source": "configs/debian-installer/includes/common", "frontends": ["text", "gtk"]},
    {"source": "configs/debian-installer/includes/gtk", "frontends": ["gtk"]}
  ],
  "remove": [],
  "patches": [],
  "generated_preseed": true,
  "generated_hooks": true,
  "archive": {"format": "newc", "compression": "gzip", "level": 9}
}
```

Recommended order: verify original; unpack; apply common/frontend overlays;
apply removals/patches; generate preseed/hooks; validate resources/modes/links;
compress to a temporary file; inspect; publish atomically; generate ISO checksums.
Define conflict rules so accidental overlays cannot remove required hooks.

## Validation proposed for the editor

- Round-trip unchanged initrds and compare content/metadata.
- Test files, symlinks, modes, removals, link replacement and multiple gzip/CPIO
  archives.
- Inspect both frontends with `lsinitramfs`, `gzip -t` and all-member inspection
  where necessary.
- Boot text/GTK, with/without dark mode, and execute early hooks before mounting CD.
- Install offline from local packages and verify final APT sources; download-only
  is not a full installation test.
- Visually inspect banners, fonts and accessibility for the selected version.
- Boot BIOS/UEFI and the installed disk after removing the ISO.

The survey did not establish a complete successful installation. It provides
inspectable references for later implementation and testing.
