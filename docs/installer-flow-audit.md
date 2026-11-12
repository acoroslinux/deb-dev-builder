# Installation flow audit — 2026-11-04

This audit covers rootfs/ISO preparation, Calamares module contracts and d-i
hooks. Fixes were checked through composition tests, script execution and
native modules extracted from packages. These checks do not replace complete
installation and installed-disk boot in a VM.

## Resulting flows

| Stage | Calamares | d-i live |
|---|---|---|
| Before ISO | Rootfs packages, modules, schemas, formatters and machine-id audited | Support packages, offline pool and udeb ABI verified |
| Copy | `unpackfs` | `live-installer`, preserving partman fstab/crypttab |
| Target preparation | `prepare-install-target` removes live autologin, sudoers and live configuration before account/login modules | Hooks isolate APT sources and remove live account/configuration; successful cleanup is marked |
| Users | `removeuser`, `users`, `displaymanager` | Native d-i questions/configuration |
| Packages/boot | Live package removal, initramfs, grubcfg, bootloader | Native GRUB installation; live package removal in finish-install |
| Validation | `validate-installed-boot` before final cleanup/unmount | `14zz-deb-dev-target-boot` before APT source restoration |
| Final preferences | Native Calamares autologin choice | Optional `deb-dev/autologin` question after target validation |

## Confirmed issues and corrections

| Issue | Correction/evidence |
|---|---|
| Cleanup deleted the empty `/etc/machine-id` created by customization | Keep an empty regular file and D-Bus link; repeated-cleanup test |
| Live `systemd-machine-id-commit` mask copied to disk | Final validation removes only the `/dev/null` mask, generates identity if needed and aligns D-Bus |
| d-i cryptsetup hook wrote literal `\n` | Corrected `printf`; test sources the generated shell configuration and checks effective `CRYPTSETUP` |
| Live autologin survived in a drop-in while LightDM changed only the main file | Cleanup before displaymanager; execute the real LightDM module with manual/new-account autologin choices |
| tty1 autologin still named the removed live user | Include it in shared cleanup before installed-user configuration |
| `gdm3`, `defaultShowUsername`, `sysconfig`, `keepDistributor` did not match module interfaces | Correct names/remove ignored options; validate installed-package schemas |
| LightDM `basicSetup` used malformed chmod in Debian 3.3.14 | Disable it; Debian/Devuan packages already create greeter accounts and permissions |
| services-systemd did not consume `timers` | Use `units`, `fstrim.timer`, action `enable` |
| fstab included mount-module options and lacked tmpOptions | Keep mount options in mount.conf and align fstab with native schema |
| bootloader ignored `installHybridGRUB` | Remove the unsupported key and capability claim |
| F2FS could be offered without its formatter; non-live variants could omit other tools | Both live installers receive filesystem support; compare offered filesystems with rootfs tools |
| ABI filtering could remove every matching kernel-module udeb | Fail before publication if indexed kernel versions exist but none matches the initrd |
| Hook failures could appear only in logs | Preparation/cleanup markers and native status 10 gates; execute real finish-install runner in tests |
| Finish-install retries lost APT preparation state after restoration | Accept persistent restored state to allow finalization retries |
| Calamares did not verify installed initrd content | Shared gate checks cryptroot, LVM, udev rules and required crypttab |
| GRUB retained live parameters or lacked Linux entries | Reject those configurations; require nonempty fstab and grub.cfg |
| Builder SSH-key cleanup could leave installed sshd without keys | Generate missing host keys when sshd exists, preserving existing keys |

Machine-id settings follow the [Calamares 3.3.14 interface](https://github.com/calamares/calamares/blob/v3.3.14/src/modules/machineid/machineid.conf):
`dbus-symlink`, explicit systemd UUID style and target entropy generation.

## Automatic audit and diagnostics

`configs/build/installer-audit.json` declares job ordering, required operations,
module directories, formatters and report path. Real ISO builds audit after
cleanup and before compression/publication. Calamares checks include:

- Required module descriptors/payloads, instances and critical ordering.
- Configuration against schemas from the actual installed package.
- Formatters for filesystems offered in the interface.
- Branding descriptors, local resources and image-format signatures.

Schema validation uses Python, PyYAML and jsonschema in the isolated toolchain,
not libraries installed into host Python. Relative internal definition
references are normalized; auditing does not download schemas.

The rootfs report is `var/lib/deb-dev-builder/installer-audit.json`. To inspect
an extracted rootfs with `requirements.txt` dependencies in the chosen Python
environment:

```bash
python3 tools/audit-installers.py /path/to/extracted-rootfs
```

Installed-target validation records START and OK in
`/var/log/installer/deb-dev-validation.log`. Specific errors remain in native
installer logs. START without OK means the gate did not finish, not proof of
which operation failed.

## Debian Installer autologin

`configs/debian-installer/autologin.json` enables an optional boolean question,
defaulting to false, for desktop builds with supported login managers. The
English template loads in S25, after S20templates and before
S30initrd-preseed. The question runs during finish-install, after account and
desktop creation; it is not an extra field in the native account-creation page.

For unattended installation:

```text
d-i deb-dev/autologin boolean true
```

Use `false` to disable it explicitly. The equivalent JSON override is:

```json
{
  "runtime": {
    "installer": {
      "autologin": {"enabled": true, "ask": false, "default": true}
    }
  }
}
```

The backend reads `passwd/username`, requires an existing account and a
configurable minimum UID, and never enables root login. It asks only if the
selected manager exists and user creation was requested. Headless builds do
not gain console autologin. LightDM, SDDM and GDM paths/sections/values are JSON.

Changing a retry answer to false disables the previous configuration. INI
updates retain unrelated settings. This does not change passwords, PAM or
sudoers and does not bypass encrypted-disk authentication.

## Validation limits and encrypted boot

The examined Calamares package was Debian 3.3.14-1. Relevant Python modules were
executed and both systemd/non-systemd sequences checked with native schemas.
This does not prove compatibility with every package version; each build audits
its own installed modules. Initramfs tests cover commands, listed content,
shell configuration, markers, failures and installer return codes. Autologin
protocol/file tests alone do not establish a real graphical session.

A real LUKS+LVM installation exposed an empty `/cryptroot/crypttab` despite
present `cryptsetup` and `lvm` binaries. Listing the file with `lsinitramfs` does
not establish unlock configuration. Before final regeneration, the builder
translates partman's `x-initrd.attach` selection to the `initramfs` option
consumed by `cryptsetup-initramfs`, preserving devices, UUIDs, keys and other
options. Selection rules are in `configs/storage/installer.json`.

Validation extracts every initrd with `unmkinitramfs`, checks explicitly selected
volumes' expected names/origins and rejects empty tables when early unlock is
required. Temporary files are removed on success or failure. Data volumes not
selected for early unlock need not appear. Tests use real initramfs archives
and Debian's selector, including empty tables and incorrect names/UUIDs.

Full runtime coverage still needs explicit BIOS/UEFI, ext4/btrfs, plain/LVM/
LUKS+LVM, separate/encrypted boot, online/offline and manual/autologin cases.
Remove the ISO and boot the installed disk. Later user-confirmed results are
recorded separately in the desktop validation guide.

## d-i language and locale

The live rootfs already has `LANG` in `/etc/locale.conf`. Native
`05localechooser` replaces `# LANG=` but preserves a populated value, allowing
live language to override the installer's selection even if generation worked.

The hook in `configs/debian-installer/locale.json` reads
`debian-installer/locale` and `debian-installer/language` from debconf, verifies
`SUPPORTED`, enables generation without removing existing locales and runs
native `update-locale --reset` to align `/etc/locale.conf` and
`/etc/default/locale`. This also removes inherited `LC_*` overrides.

It applies to live/netinstall/netboot, text and GTK, without hardcoding a
language or replacing installer choices with build defaults. The applied
selection is logged in `/var/log/installer/deb-dev-locale.log`; failures stop
finalization with native status 10. Tests execute Debian's real `update-locale`
with several locales, builtin C locales, repetitions and invalid selections.
Project-authored copy remains English; native localization stays available.

## GRUB and Plymouth

`configs/customization/grub.json` owns installed-system defaults: selected
entry, menu timeout, distributor, resolution, framebuffer retention, recovery,
submenus and other-system detection. Existing extra arguments are retained,
as are installer-owned `GRUB_ENABLE_CRYPTODISK` and `GRUB_CMDLINE_LINUX`.
`quiet` is deduplicated; `splash` is added when Plymouth/theme exist. GRUB themes
are selected only when their files exist.

d-i reapplies these values before final storage validation, selects Plymouth
and regenerates GRUB. Live-mode initramfs regeneration belongs to the following
storage step; other modes regenerate in the hook itself. Calamares receives
the same JSON values through generated native `grubcfg` configuration.

`configs/images/iso.json` controls live Plymouth kernel arguments. The generator
preserves `quiet`/`splash` and adds configured arguments when rootfs has Plymouth
and its theme. A build without Plymouth does not automatically receive `splash`.

`configs/customization/plymouth.json` selects themes/commands and required
installed-initramfs files/plugins. Final validation requires daemon, client,
script/text plugins and theme resources. The theme supports messages, questions
and masked passwords. Those checks do not replace graphical boot tests.

Background layout is recalculated when the screen becomes available or changes
size. Zero initial dimensions never create zero-sized images. Background
visibility does not depend on an opacity animation. Native Debian script/image/
sprite tests use the actual PNG and simulate display dimensions/availability,
covering delayed screens, resolution changes and unlock prompt lifecycle.

A graphical test found the actual black-background cause: the former
`configs/assets/background.png` contained JPEG bytes copied to `splash.png`.
The native engine returned a null image. Earlier tests used the original
`custom_files` PNG and missed this replacement. PNG/JPEG assets now have real
matching formats and separate destinations in `theme_mapping.json`. Signature
checks reject mismatches. A test applies the real mapping and decodes the result
with native Plymouth.

The theme has a gradient, subdued background, distribution identity, animated
dots, progress and separate unlock/message areas. It remains usable without the
optional image. Text, fonts, colors, positions and animation parameters are
rendered from JSON `visual` before theme selection/initramfs regeneration.
Rendering is idempotent. A temporary QEMU VM exercised the real DRM renderer,
ISO kernel/modules and masked-password dialog; that alone did not establish
VirtualBox compatibility.

## Live reboot medium-removal prompt

`configs/customization/live-media.json` controls presentation of the native
`live-tools` prompt. The builder preserves live/USB/netboot/toram/findiso/noeject
checks, device selection, eject and console fallback. Native blocks are checked
before patching; incompatible versions fail the build.

The default presentation is `retained`. Under systemd, a service after native
`plymouth-poweroff/reboot/halt` services and before `final.target` invokes
`live-medium-eject` in preparation mode. It reuses native media/device checks
but exits before ejecting. The helper sends the prompt to the still-working
renderer and records its VT in `/run`. Plymouth's native retention mechanism
keeps that image while processes exit.

The later script invocation retains that renderer and selects the same VT,
without clearing the screen or drawing into a different framebuffer. It
explicitly restores keyboard mode, including leaving Plymouth's disabled mode,
and reads ENTER directly without deallocating the terminal. Instead of
`stty sane`, which failed on that VT in a real test, it sets only necessary
read/echo options and tolerates partial terminal-configuration failure.
It neither restarts the daemon nor discovers keyboards through udev. Without
preparation state, the prior graphical path and native console fallback remain.
Early preparation is systemd-specific; other inits retain their existing path.

Terminal command, keyboard mode, services, state path and stage-variable name
are JSON. Xorg packages explicitly include `kbd` for `openvt`, `fgconsole` and
`kbd_mode`. Theme copy supplies prompt text. A full shutdown test showed that
switching to a new console could also produce a black screen; that attempt was
removed. This revision replaces the `plymouth.ignore-udev` experiment that lost
ENTER in VirtualBox and does not add it by default. Control commands have
timeouts; ENTER waiting has no additional timeout, though native init limits
still apply.

Both installers remove the helper, early service and links from installed
systems. Tests cover native callbacks, boot/shutdown/reboot, service setup/
cleanup, confirmation on the retained VT after daemon exit, isolated keys and
ENTER. Rebuild the ISO to distribute changes; VirtualBox validation requires
that rebuilt artifact.

### desktop-base comparison

Examined reference: `debian-desktop-team/desktop-base`, commit
`84c8fb3ff7b9558e012988fd0a961dc4e9229873`, including
[`ceratopsian.plymouth`](https://salsa.debian.org/debian-desktop-team/desktop-base/-/blob/84c8fb3ff7b9558e012988fd0a961dc4e9229873/ceratopsian-theme/plymouth/ceratopsian.plymouth),
[`ceratopsian.script`](https://salsa.debian.org/debian-desktop-team/desktop-base/-/blob/84c8fb3ff7b9558e012988fd0a961dc4e9229873/ceratopsian-theme/plymouth/ceratopsian.script),
`Makefile` and `debian/postinst`.

Its `script` module, `ImageDir` and `ScriptFile` match our theme format. Both
register refresh callbacks. Ceratopsian's `SetMessageFunction` is a native alias
for `SetDisplayMessageFunction`; renaming it does not fix an invisible prompt.
Its empty quit callback has no special framebuffer recovery. Package theme/
alternative setup is not a late-shutdown keyboard/renderer solution.

Theme tests now initialize native `script_lib_plymouth` and fire message,
normal, refresh and hide events through it in boot/shutdown/reboot modes,
covering actual callback registration as well as direct functions. This alone
does not establish VBox driver presentation.

The comparison also examined
[`joy.script`](https://salsa.debian.org/debian-desktop-team/desktop-base/-/blob/84c8fb3ff7b9558e012988fd0a961dc4e9229873/joy-theme/plymouth/joy.script)
and `joy.plymouth`. Joy uses the same module/message alias, continuous refresh
and message restoration after password dialogs. Its quit callback retains the
logo and completes progress; it neither ejects media nor recovers a dead
renderer. Retention must be coordinated with prompt timing, not copied images
or callback names.

A final QEMU test booted the existing ISO with a diagnostic overlay applying
current files, then requested systemd poweroff. Preparation recorded VT 63.
After eject, the captured theme showed the prompt, direct VT input accepted
ENTER and logs confirmed `Powering off` without another confirmation. The
original ISO remained unchanged. That test itself was not VBoxSVGA validation.

## Single-prompt encrypted Calamares layouts

Both native sequences run `luksbootkeyfile` after unpacking and before `fstab`.
`configs/storage/installer.json` owns the keyfile paths, commands and private
permissions; `configs/customization/calamares.json` owns the generated job paths.
The native module generates and enrolls a random key for encrypted root/swap
volumes, and skips key creation when a separate `/boot` is unencrypted.
The generated `configure-encrypted-unlock` job then checks that `/boot` has a
cryptographic block-device ancestor, verifies the key against every referenced
LUKS device, and configures Debian/Devuan's documented `KEYFILE_PATTERN` support.
Keys are owner-only and initramfs-tools receives `UMASK=0077`. Participating
boot volumes are explicitly selected with `initramfs`; `noauto` is preserved.
The job refuses an incompatible existing key pattern rather than overwriting it.

With `/boot` inside encrypted root, GRUB still asks for the passphrase once.
After loading the protected initramfs, cryptsetup uses the enrolled random key
for the participating root/swap mappings. This does not disable encryption or
put the key on the unencrypted EFI system partition. Separate unencrypted
`/boot` layouts do not receive this embedded-key mechanism. No passphrase is
stored in a script, and no disk key is generated during ISO construction.

Final validation extracts each relevant initramfs, checks owner-only image
permissions, and compares embedded per-mapping keys with the target key without
printing key material. Missing keys, mismatched devices and unenrolled keys
stop installation. This integrates initramfs-tools rather than systemd and
therefore applies equally to the two Calamares init variants. The d-i's native
partitioning/key-creation mechanism is unchanged.
Configured backup/temporary initramfs suffixes are excluded from active-image
validation; old backup files must not be mistaken for installed kernel images.

The Plymouth theme keeps a visible entry border and background even with zero
password characters. Normal, repeated and subsequent-device password callbacks
reset/recreate the entry state; only masked characters are displayed. A common
hint explains Enter submission. The live-medium-removal path is unchanged.

References: [Calamares LUKS key generation](https://github.com/calamares/calamares/blob/v3.3.14/src/modules/luksbootkeyfile/LuksBootKeyFileJob.cpp),
[Debian initramfs keyfile integration](https://cryptsetup-team.pages.debian.net/cryptsetup/README.initramfs.html).
These changes require a rebuilt ISO and a new encrypted-installation boot test.
