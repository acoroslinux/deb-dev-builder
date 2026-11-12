# Improvements implemented from the live-build reference

This page documents implemented features. The [d-i investigation](live-build-reference.md)
and [additional mechanisms](live-build-project-reference.md) preserve the earlier
research; their proposals are not all capabilities of this builder.

## Configuration and compatibility

The policies use the existing catalog:

| File | `runtime` override target |
|---|---|
| `configs/build/pipeline.json` | `build` |
| `configs/debian-installer/customization.json` | `installer.customization` |
| `configs/images/iso.json` | `artifacts.iso` |
| `configs/images/squashfs.json` | `artifacts.squashfs` |

Overrides follow normal profile composition. Explicit runtime lists replace
previous lists. There are still exactly three public hook stages:
`pre-chroot`, `chroot`, and `post-chroot`.

## Debian Installer initrd editor

The editor supports gzip and CPIO `newc`, including concatenated archives and
the checksum CPIO variant. It processes entries in memory without extracting
archive paths onto the host filesystem. The last entry for a path wins.
Hardlinks are materialized to avoid mixing inode numbers from different
archives. Modes, UID/GID, timestamps, symlinks and device identifiers are
preserved; the repacked result does not preserve hardlink relationships.

Text and GTK initrds start from the official artifacts downloaded into each
build's staging directory. Processing proceeds as follows:

1. Read every gzip/CPIO member and validate paths and data.
2. Apply declared overlays in order, selected by frontend.
3. Apply removals and text replacements with exact occurrence counts.
4. Add required generated files, which take precedence.
5. Repack, read back and compare entries before atomic publication.

`enabled: false` disables optional overlays, removals and patches. Live d-i
still receives its required preseed and offline-installation/cleanup hooks.
They are no longer simply appended as another compressed archive.

The default policy now enables the GTK-only overlay in
`configs/debian-installer/includes/gtk`. It installs the DebDevModern GTK2
theme and native PNG banners. The text frontend retains its original theme.

Example override, after creating the referenced source trees:

```json
{
  "runtime": {
    "installer": {
      "customization": {
        "enabled": true,
        "overlays": [
          {"source": "configs/debian-installer/includes/common", "frontends": ["text", "gtk"], "uid": 0, "gid": 0},
          {"source": "configs/debian-installer/includes/gtk", "frontends": ["gtk"], "uid": 0, "gid": 0}
        ],
        "remove": [],
        "patches": [
          {"path": "etc/gtk-2.0/gtkrc", "search": "DebDevModern", "replace": "dark", "count": 1, "frontends": ["gtk"]}
        ],
        "compression_level": 9
      }
    }
  }
}
```

Trees are relative to the initrd root. GTK banners can use the paths identified
in the d-i guide. A replacement with an unexpected match count fails the build
and retains the previous initrd. A path cannot have children when its final
entry is a file or symlink. To replace a directory with a link, remove its
children explicitly first.

`early_command.sh` is embedded and referenced by its internal initrd path,
without depending on `/cdrom` being mounted. Network modes embed
`late_command.sh` the same way. The legacy script no longer creates sudoers
for fixed usernames or indiscriminately copies `custom_files` into `/target`.

Live d-i embeds the preseed selecting live installation. Other modes require
explicit `--preseed` or `runtime.installer.customization.embed_preseed: true`
to embed answers; the external file remains available. Embedded scripts do not
execute until the corresponding preseed is selected. Netboot packaging also
customizes initrds in the official tree while preserving links.

When preparing the d-i pool, the original initrd's `lib/modules` version filters
out kernel/module udebs for other versions. Package-name patterns are
configurable in `media.json`. If module information is absent, the builder does
not infer an ABI from the live kernel.

## Installed-system and live-only packages

`build.packages.live_only` selects packages for the second installation pass.
The first installs the remaining packages and records the system inventory;
the second installs live components and their dependencies. A profile's
`live_packages` can explicitly add packages to that second pass.

The difference between installed identities produces `live_remove_packages`,
used by both d-i's `filesystem.packages-remove` and generated Calamares
`packages.conf`. A version change does not turn a system package into a live
package. `build.packages.keep_installed` protects package names from removal.
Only `Status: install ok installed` entries count, including a final paragraph
without a trailing blank line.

This difference is computed after package installation and before customization
hooks. Packages installed later by scripts are not automatically included in
that difference; prefer declaring them in profiles.

## Local DEBs, multiarch and build-only sources

```json
{
  "runtime": {
    "build": {
      "packages": {
        "foreign_architectures": ["i386"],
        "local_debs": ["packages/example-package_1_all.deb"],
        "build_only_sources": ["deb https://mirror.example/debian trixie main"]
      }
    }
  }
}
```

Architectures are registered before APT updates. Local DEB metadata is read
with `dpkg-deb`; packages are indexed with `apt-ftparchive` in the isolated
toolchain and installed through APT. Their architecture must be `all`, the
primary architecture, or a declared additional architecture. Local trust applies
only to the temporary source for the selected DEBs.

The local repository and build-only sources are removed before artifact
creation. Normal profile sources remain system sources. This mechanism is
separate from the ISO's offline pool. Bootstrap cache identity also includes
distribution, mirror and components; matching suite and architecture alone
cannot make an old/incompatible cache valid.

## ISO, Secure Boot and reports

- `artifacts.iso.secure_boot`: `auto`, `require`, or `disable`. Required mode
  needs a GRUB UEFI ISO and signed binaries for the primary platform.
- `signed_loaders`: platform-specific paths and names, including amd64, i386
  and arm64. Copies shim, GRUB and MokManager when available.
- `binary_overlays`: explicit `source`/`destination` trees. Destinations are
  relative to the ISO root; `.` selects that root. The complete
  `configs/custom_files` tree is no longer automatically duplicated outside
  SquashFS.
- `checksum_algorithms` and `checksum_excludes`: internal checksums computed
  after the offline repository and overlays. Defaults exclude only boot files
  actually modified and checksum files themselves; other isolinux files are
  verified.
- `.disk/contents`: staging path inventory. The El Torito catalog created later
  by xorriso is not part of that inventory.

With `build.report.enabled`, every artifact receives a `<name>.build.json`
containing SHA-256, size, selected public configuration fields, inventories and
tool versions queried inside the isolated toolchain. Defaults do not publish
user profiles or script/preseed content; `configuration_fields` controls the
selection. Reports are produced after final hooks and do not claim a successful
boot or installation test.

Early validation includes `build.capability_rules` alongside format,
architecture and installer checks.

## Compression and content timestamps

`artifacts.squashfs` supports `processors` (`0` for automatic selection),
`compression_level` and `sort_file`, in addition to compressor and exclusions.
Exclusions support wildcards; progress output is disabled without a terminal.

`build.reproducibility.source_date_epoch` accepts `null` or an integer timestamp.
When set, it controls edited-initrd timestamps, ISO timestamps and metadata,
SquashFS time options, FAT identity and tar archives prepared by ISO/netboot
engines. Tarballs are published through temporary files. Development artifact
names remain unique.

This prepares deterministic content along those paths but does not pin package
versions or remove every source of distribution-wide variation. It does not
guarantee reproducibility for all disk/OCI images or constitute a Secure Boot
test with real firmware.

## Remaining reference work

Dracut, dm-verity with a trust chain, rebuilding the d-i frontend, automatic
firmware discovery, zsync distribution and source images remain separate work.
They require their own execution contracts and tests. No unsupported switches
were added for those capabilities; the improvements above use existing
backends and installation flows.
