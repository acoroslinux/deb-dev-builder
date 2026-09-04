# Getting started

## Host setup

Use Debian or Ubuntu and install the host dependencies with:

```bash
sudo ./setup_host_build_env.sh
python3 -m pip install -r requirements.txt
```

If the native host lacks the tools required by the selected format, the builder
can bootstrap an isolated Debian build-host. `--force-isolated-toolchain` always
uses that path.

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
