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

Live accounts are locked by default and root remains locked. Desktop autologin
does not require a password. If a custom profile sets `live_user.password`, the
builder sets that password only for the named live account; never use a default
or shared password in production media.

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

Bare output names go to `output/`. Absolute paths and relative paths containing
a directory are respected. Existing artifact suffixes are replaced, so
`--format vdi --output images/machine.qcow2` creates `images/machine.vdi` before
optional compression.
