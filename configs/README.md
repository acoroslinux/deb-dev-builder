# Configuration ownership

Start with `catalog.json`: it is only a document-loading index. The builder
composes those JSON documents and derives architecture/kernel/init lookup maps
from the existing profiles. There is no separate `runtime.json` to synchronize.

| Change | Edit |
|---|---|
| One build's hostname, locale, hooks or toolchain override | `global_build.json` or the JSON selected with `--config` |
| CLI defaults | `build/cli.json` |
| Automatic software profiles and build tools | `build/policy.json` |
| Artifact filenames, build identity and naming labels | `build/artifact-naming.json` |
| Architecture aliases, kernel suffix, EFI targets or image packages | `architectures/<arch>.json` |
| Kernel package templates | `system/generic.json`, `system/rt.json`, `system/cloud.json` |
| Init packages, live-config or zram dependency | `system/<init>.json` |
| Distribution mirrors, suite or repository components | `system/<distro>.json` |
| Bootloader profiles or common boot policy | `boot/<loader>.json`, `boot/policy.json` |
| Live credentials and groups | `live-users/<account>.json` |
| Account shell and permissions | `live-users/policy.json` |
| Optional package groups | `software/<group>.json` |
| Optional service profiles | `services/<profile>.json` |
| Automatic services, masks and aliases | `services/policy.json` |
| Desktop packages/session type | `desktops/<desktop>.json` |
| Common session detection and PAM fallback | `desktops/session-defaults.json` |
| Manager-specific login templates | `display-managers/<manager>.json` |
| SquashFS, ISO or disk geometry/compression | `images/<format>.json` |
| Isolated build host, dependency packages or keyring pins | `toolchain/defaults.json` |
| Branding, overlays, themes, Samba, zram, Flathub or polkit | The corresponding `customization/<subject>.json` |
| Live installer shortcuts and installed-target cleanup | `customization/calamares.json` |
| Live offline APT source and temporary installer source backup | `customization/offline-repository.json` |
| Device firmware/boot artifacts or VM settings | `hardware/<device>.json`, `vm/<hypervisor>.json` |
| NVIDIA package availability by release | `drivers/<release>.json` |
| Additional rootfs files and vendor themes/icons | `custom_files/` |
| Shared artwork destinations | `assets/theme_mapping.json` |
| GRUB/syslinux menu content | `boot/templates/` |
| Shell build extensions | `hooks/` |
| Debian Installer answers or Calamares settings | `debian-installer/`, `custom_files/calamares/` |
| Debian Installer live/netinstall/netboot integration | `debian-installer/media.json` |

Module documents listed in `catalog.json` are not selectable profiles. Adding a
new architecture or kernel modifies/adds its profile, not a second lookup map.
Adding a new module document requires an entry with a relative `source` and a
`target` in the composed configuration; dotted targets create nested objects.
An optional `key` selects one object from a shared source document.

Global `runtime` overrides remain supported. Objects merge recursively; lists
inside `runtime` replace the module defaults, including an explicit empty list.
Normal profile package lists continue to merge without duplicates.

Native theme, boot, preseed, shell and installer files retain the format their
consumer requires. In particular, installed-system partition/account policies
are separate from live-media and raw disk-image defaults. Avoid copying their
contents into a second JSON representation.
