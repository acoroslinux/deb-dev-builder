# Additional live-build mechanisms useful to this project

See [implemented improvements](live-build-applied.md) for subsequent changes.
This survey complements the [Debian Installer investigation](live-build-reference.md).
It examined `/iso-builder/live-build` at commit
`531cdb9854e04d08c3c3770463454d8a5e91b129`. References below are paths within
that clone. They describe examined code, not guarantees that every combination
works or features automatically available in this builder.

Suggested applications were future work at the time of this investigation.
This survey does not execute upstream hooks, replace build engines or introduce
a runtime dependency on live-build.

## Reuse map

| Subject | Main reference | Potential application |
|---|---|---|
| Build ordering | `scripts/build/chroot`, `binary` | Explicit internal dependencies without multiplying public hook stages |
| Live/installed packages | `chroot_package-lists`, `binary_manifest` | Inventory-derived live-package cleanup |
| Repository contexts | `chroot_archives`, `functions/sourcelist.sh` | Separate build sources from published system sources |
| Local packages | `chroot_archives` | Install custom DEBs through APT |
| Multiarch | `chroot_package-lists`, `chroot_install-packages` | Declare additional package architectures |
| Cache/resume | `bootstrap_cache`, `chroot_cache`, `functions/cache.sh` | Validate prerequisites and distinguish completed stages |
| Mounts/failures | `functions/exit.sh`, `chroot_sysv-rc` | Predictable cleanup and service-policy restoration |
| Overlays | `chroot_includes_*`, `functions/chroot.sh` | Explicit ordering, ownership and link handling |
| Firmware | `functions/firmwarelists.sh`, `chroot_firmware` | Architecture/archive-content selection |
| Alternate initramfs | `share/hooks/normal/5050-dracut.hook.chroot` | Adapt paths, installers and jobs together |
| UEFI/Secure Boot | `binary_grub-efi`, `efi-image`, `grub-cpmodules` | Declare required support and verify published binaries |
| SquashFS | `binary_rootfs` | Profile-specific ordering, exclusions and compression |
| Inventories/checksums | `binary_manifest`, `binary_checksums` | Content diagnostics and media verification |
| Reproducibility | `binary`, `binary_iso`, `examples/hooks/reproducible/` | Separate development identity from reproducible content |
| Network/disk images | `binary_netboot`, `binary_hdd`, `binary_tar` | Separate format contracts |
| Rootfs integrity | `binary_dm-verity` | Future hash-tree and boot verification support |
| Incremental publishing | `binary_zsync` | Auxiliary files for distributed ISO updates |
| Package sources | `source_debian` | Record source versions and unavailable sources |
| Validation | `functions/configuration.sh`, `debian/tests/`, `Makefile` | Reject incompatible combinations and test distinct levels |

## Installed-system and live-only packages

`scripts/build/chroot` installs in `install` and `live` passes. It records
`chroot.packages.install` after the first and `chroot.packages.live` after
customization. `chroot_package-lists` selects common lists and
`.list.chroot_<pass>` lists. `binary_manifest` publishes
`live/filesystem.packages`, derives `filesystem.packages-remove` from the
inventory difference and publishes inventories outside the image.

**Proposed application:** declare live-only packages in JSON and derive one
cleanup policy for d-i and Calamares. Compare package identities, explicitly
handling architectures and version changes rather than copying upstream's
`diff | awk`. A dependency needed by the installed system must not become
removable just because it was added during the live pass.

At the time of the survey, this builder already generated
`filesystem.packages`, an external manifest and a configured d-i removal list.
That was not yet the two-inventory model described above.

## Build and runtime repositories

`chroot_archives` installs sources, preferences, APT configuration, authentication
and keys for the build context. Cleanup removes `.chroot`-only resources and
prepares `binary` resources, mounting local `file:` sources when necessary.
Upstream distinguishes bootstrap, chroot, security, final-image and installer
mirrors so a local build mirror need not leak into the delivered system.

**Proposed application:** represent contexts in JSON and validate final sources
in the toolchain, live rootfs, d-i and `/target`. Existing offline policy covers
part of this separation, not all upstream pinning/key/source management. Use
supported APT formats and explicit key scope instead of importing legacy
mechanisms automatically.

### Local DEBs and multiarch

Upstream gathers architecture-specific and `_all.deb` packages into temporary
`/packages`, generates `Packages`, `Packages.gz` and `Release` with
`apt-ftparchive`, installs through APT and removes the repository afterwards.
This is distinct from the offline pool published on the ISO.

Package lists accept `package:architecture`. The pipeline discovers additional
architectures, calls `dpkg --add-architecture` and updates indexes before
installation. This differs from building another architecture's image using
QEMU.

**Proposed application:** APT-resolved local-DEB/additional-architecture profiles,
with useful missing-package/version errors and preserved dependency handling.
Upstream package-list syntax supports shell commands and conditionals; our
composition should remain declarative JSON, not acquire a second language.

## Cache, resume and mutual exclusion

Bootstrap, chroot, packages and indexes have separate caches. Bootstrap restore
checks that DEBs required by certain d-i modes remain available, rejecting the
cache otherwise. `functions/cache.sh` uses hardlinks on the same filesystem and
copies across filesystems.

`functions/stagefile.sh` records dependencies/completion in `.build`; those
markers are execution records, not complete configuration fingerprints.
`functions/lockfile.sh` checks and creates a lock file. It should not replace
our atomic `flock` locking with a check-then-create sequence.

**Proposed application:** store relevant configuration, suite, architecture,
versions and dependencies with caches. Invalidate consumers when producers
change, and verify required artifacts rather than directory presence alone.
The existing toolchain/rootfs/APT caches can benefit from stronger validity and
resume rules. Hardlinks are appropriate only for immutable content: editing
staging must not change the original cache.

## Chroot, overlays and failure recovery

`functions/chroot.sh` runs commands with `env -i` and an explicit environment.
`chroot_sysv-rc` blocks service startup through `policy-rc.d`, removing its
alternative afterwards. `functions/exit.sh` installs traps, attempts unmounts
and preserves packages after failures.

The Python builder already handles temporary service policies, mounts and
cleanup. Use upstream to review interruptions, busy mounts and temporary
configuration leakage, not to replace these mechanisms with shell traps.

Upstream distinguishes pre-package and post-package overlays. `Chroot_copy_dir`
uses rsync ownership/link options or tar inside the chroot with
`--keep-directory-symlink`; these paths need not preserve identical metadata.

**Proposed application:** document precedence against packages and declare
paths, modes, ownership and link policy in profiles. Retain the three public
hook stages. `chroot_hooks`' read-only configuration bind mount is useful if
scripts need access without permission to change the source.

## Firmware, kernel and initramfs backends

`Firmware_List_From_Contents` reads `Contents-all.gz` and architecture indexes
per component/suite. It selects packages containing `lib/firmware` or
`usr/lib/firmware`, deduplicates and applies architecture-family exclusions and
other policies. Chroot and installer consume these results.

**Proposed application:** combine configurable lists with architecture selection
and documented exclusion reasons. Do not copy another suite's blocklist
without review. Firmware in live, firmware detectable by d-i and kernel modules
inside an initrd are different checks.

`5050-dracut.hook.chroot` demonstrates that dracut changes require moving the
live-media mount from `/run/live/medium` to `/run/initramfs/live`, adapting
Calamares `unpackfs`, replacing initramfs jobs and reviewing removed packages
and APT helpers.

**Proposed application:** a JSON-backed backend contract supplying all consumer
values. Changing only the generator package leaves incorrect installer paths.
The cryptsetup hook also shows why binary presence alone is insufficient;
initramfs content must be prepared and verified against the package version.

## Hybrid boot, UEFI and Secure Boot

`binary_grub-efi` distinguishes `auto`, `enable` and `disable`. Required mode
needs signed shim/GRUB; automatic mode diagnoses missing components and the
limited shim-only case.

`efi-image` builds separate local-media and network GRUB binaries. Local boot
finds `/.disk/info` before loading ISO configuration. It builds FAT with mtools
and copies GRUB modules/fonts, distinguishing EFI names/platforms by architecture.
Some amd64 images also include 32-bit UEFI support.

**Proposed application:** declare required/optional Secure Boot, verify binaries
actually shipped and test the boot chain. Listing `shim-signed` is not proof of
that chain. Existing BIOS/UEFI output and conditional signed-binary copying do
not replace a Secure Boot test. Media identity must match labels/menus; network
boot needs its own prefixes/modules rather than blindly reusing local EFI.

## Rootfs integrity and inventories

`binary_rootfs` supports `squashfs.sort`, exclusions, compressor and level, and
suppresses progress without a terminal. Compression/exclusions already exist;
profile-specific ordering can improve boot reads.

`binary_checksums` sorts with a stable locale, uses NUL-terminated names and
excludes checksum files and selected boot images that will change. It supplies
verification instructions and a `.contents` inventory. Binary-tree hooks run
before internal checksums, unlike the external ISO hash generated after our
final hooks.

**Proposed application:** match exclusions to files xorriso actually changes;
avoid broad exclusions. Add useful comparison inventories and JSON reports
with resolved configuration, tool versions and artifact identities. A package
manifest alone is not a complete SBOM.

`binary_dm-verity` produces a hash tree, roothash, optional FEC and optional
external-script signatures. It needs matching initramfs support and a roothash
trust policy. This is separate future work, distinct from Secure Boot and
transport checksums.

## Reproducibility and unique development names

`functions/configuration.sh` establishes `SOURCE_DATE_EPOCH`; `binary` normalizes
recent timestamps and `binary_iso` supplies that date to xorriso. `efi-image`
derives FAT identity from the same reference.

Example reproducibility hooks address font/AppStream caches, UUIDs, initrds and
package-generated files. They are optional examples, not automatic guarantees;
some interfere with generators and require corresponding cleanup. Do not copy
them wholesale.

**Proposed application:** retain unique development names while allowing a
separate release-content date. Pin versions/origins/configuration, build twice
in isolation and compare. Renaming an ISO changes no bytes; random internal
metadata does. Sanitization hooks also suggest reviewing machine IDs, SSH
keys, logs, adjtime, persistent device rules and generated certificates. Each
cleanup must retain a first-boot regeneration path.

## Network, disks, publishing and sources

`binary_netboot` packages `tftpboot` and live trees, moving kernel/initrd to
TFTP. It does not configure DHCP/TFTP, HTTP or NFS itself. Network output
format and d-i operating mode remain separate choices.

`binary_hdd` illustrates sizing, partitioning, loop devices, formatting, copying
and bootloader installation. Its limitations/legacy mechanisms are references
for operations and cleanup, not guarantees about our GPT/UEFI or other-platform
images. `binary_tar` is another backend, not a bootable-disk guarantee.

`binary_zsync` creates control files after the image, limited in the examined
code to ISO/hybrid ISO. It may later reduce download traffic. URLs must identify
the published artifact under its final name.

`source_debian` obtains `${source:Package}=${source:Version}` from installed
packages, downloads corresponding sources and records missing ones. This is
useful release traceability when mirrors no longer retain versions. Keeping
builder configuration is not the same as preserving source packages.

## Validation and suggested sequence

`functions/configuration.sh` checks bootloader, image type, filesystem,
initramfs and architecture combinations. Some restrictions belong to that
implementation, not all builders. The useful idea is declarative capabilities
and early errors before downloads/builds.

The `Makefile` checks shell syntax and bashisms.
`debian/tests/build-default-image` validates options and builds an image; it
does not demonstrate complete installation and installed-disk boot.

Suggested implementation order:

1. Consolidate repository contexts and verify complete offline d-i installation,
   including GRUB and installed-system boot.
2. Unify live-package declarations and d-i/Calamares cleanup.
3. Implement the initrd editor from the dedicated d-i guide.
4. Improve build reports, cache validity and capability validation.
5. Expand runtime boot/install tests across desktops and formats.
6. Evaluate reproducibility, dracut, dm-verity, source preservation and zsync
   according to release needs.

This was a proposal, not a statement of implemented scope. Keep build,
integrity, live boot, installation and installed-disk boot as distinct results:
success in one stage does not establish the following stages.
