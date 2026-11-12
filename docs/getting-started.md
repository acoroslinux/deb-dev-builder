# Getting started

## Host setup

Use Debian or Ubuntu and install the host dependencies with:

```bash
sudo ./setup_host_build_env.sh
python3 -m pip install -r requirements.txt
```

Real builds always prepare an isolated Debian build host from a verified cache
or the official OCI image. Build and artifact commands do not fall back to
native tools when that environment is unavailable. The setup script is an
optional convenience for provisioning a host; cross-architecture builds also
require working QEMU/binfmt handlers. See [the toolchain guide](toolchain.md).

## Validate first

```bash
python cli.py amd64 --distro debian-12 --desktop xfce --validate
python cli.py amd64 --distro devuan-5 --init-system openrc --desktop xfce --mode mock
```

Devuan defaults to `sysvinit`; Debian defaults to `systemd`. A missing or invalid
selected JSON profile is an error instead of being silently ignored.

### 32-bit x86 builds

`i686` is an alias for Debian's `i386` package architecture. Standalone bootable
32-bit builds currently use **Debian 12 Bookworm** or **Devuan 5 Daedalus**.
[Debian 13 and later](https://www.debian.org/releases/trixie/release-notes/issues.html#reduced-support-for-i386)
and [Devuan 6 and later](https://files.devuan.org/devuan_excalibur/Release_notes.txt)
provide compatibility packages without an official i386 kernel/installer.
The builder rejects those bootable combinations during validation.

The default kernel is `linux-image-686-pae`, requiring a PAE-capable CPU.
The `generic` and `rt` flavors are available; `cloud` has no native i386
metapackages and is rejected. These restrictions are declared by
`image.bootable_suites` and `image.kernel_flavors` in
`configs/architectures/i386.json`. Custom native sources can override the runtime
architecture policy explicitly; compatibility packages alone are insufficient.

Start with XFCE and a BIOS VM. Hybrid media also include the IA32 UEFI loader
`BOOTIA32.EFI`; testing that path requires 32-bit UEFI firmware.

```bash
python3 cli.py i686 --distro debian-12 --variant live --desktop xfce \
  --with-debian-installer --validate
sudo python3 cli.py i686 --distro debian-12 --variant live --desktop xfce \
  --bootloader grub2-hybrid --format iso --mode real \
  --with-debian-installer --recreate-seed --with-zram --tmpfs --clean
```

Replace `debian-12` with `devuan-5` for Daedalus, or add `--with-calamares` to
include Calamares alongside d-i. On 2026-11-12, the configured XFCE package sets
and offline bootloader packages were present in both official i386 archives,
and the installer manifests' `6.1.0-50-686` / `6.1.0-50-686-pae` kernels had
matching module udebs. Mock ISO pipeline checks passed; real build and VM boot
validation are still required.

Package and service profiles may be repeated or supplied comma-separated. Live
user profiles are selected separately:

```bash
python cli.py amd64 --distro debian-13 \
  --package-profile development,network-tools \
  --service-profile base,network \
  --live-profile admin --validate
```

Use the `guest` live profile for a user without administrative (`sudo`) group
membership. Device profiles other than `generic-uefi` are currently marked
experimental and print a warning before validation or build.

Live account passwords come from the `live_user.password` field of the selected
JSON configuration. The builder never prompts for a password. For
`--live-profile admin`, configure `configs/live-users/admin.json`; for
`--live-profile guest`, configure `configs/live-users/guest.json`. Without a
named profile, the `liveuser` defaults come from
`configs/live-users/liveuser.json` and can be overridden in the global build
configuration. The bundled passwords match the account names: `liveuser`,
`admin` and `guest`. See the profile format in `configs/live-users/README.md`.

A configured password is required for real desktop builds so manual LightDM
login and screen unlocking work even with autologin enabled. If it is missing,
the build fails before changing the workdirs and identifies the configuration
to edit. The password is sent to `chpasswd` on standard input and is not printed
in build logs or passed as a command-line argument. Root remains locked;
headless images without a configured live password retain a locked live account.

If an existing live session has a locked account, run `sudo passwd liveuser`
(substitute the selected account name) before locking the screen. This fixes
only that running session unless persistence is enabled; rebuild with a live
password to fix the ISO. The builder preserves the packaged display-manager
PAM rules and the system's shadow/password-helper permissions. The XFCE
light-locker uses [LightDM authentication](https://github.com/the-cavalry/light-locker)
for screen unlocking.

Guest profiles receive no passwordless sudo rule. Normal display-manager PAM
authentication is preserved; passwordless PAM configuration is confined to
autologin services. Calamares privileges are restricted to active local sudo
users and its removal module follows the selected live-user name.
The generated live-config settings preserve that username and group selection
at first boot and disable its account-creation and sudo components, since the
builder has already configured them. See the
[live-config reference](https://manpages.debian.org/trixie/live-config-doc/live-config.7.en.html).

`--with-flathub` imports the official Flathub repository descriptor and signing
key during construction, so it requires network access at that stage.

Use a VM profile to select its native disk format and guest utilities:

```bash
python cli.py amd64 --distro debian-13 --vm-profile qemu --validate
python cli.py amd64 --distro debian-13 --vm-profile hyperv --validate
```

## Real builds

```bash
sudo python cli.py amd64 --distro debian-12 --desktop xfce --mode real --format iso
sudo python cli.py amd64 --distro devuan-5 --init-system openrc --mode real --format qcow2
sudo python cli.py amd64 --distro debian-13 --di-mode netinstall --preseed server --mode real
```

Bare output prefixes go to `output/`. Absolute paths and relative paths containing
a directory are respected. `--output` supplies a prefix, with any existing artifact
suffix removed. Every build appends its distro, image type, architecture, installer,
desktop, variant, init system, kernel, bootloader, filesystem, execution mode and a
unique UTC timestamp/random build ID. The format extension and optional compression
suffix follow this name. This also applies to custom output prefixes, so repeated
builds preserve previous artifacts.

For example, an XFCE live ISO can be named
`deb-dev-debian-13-live-iso-amd64-calamares-xfce-live-systemd-generic-grub2-hybrid-ext4-real-20261031T130000Z-a1b2c3d4.iso`.
Naming templates, labels and defaults live in `configs/build/artifact-naming.json`.
Checksums and manifests use the resulting artifact name.

ISO builds write to a temporary file beside the destination and replace the
published ISO only after xorriso succeeds. Failed builds preserve the previous
ISO, and existing open readers keep the previous file contents. To test a rebuilt
ISO in VirtualBox, fully power off the VM and start it again with the new image;
resuming a saved live session can retain cached data from the previous image.
