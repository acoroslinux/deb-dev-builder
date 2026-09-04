# Installers

## Calamares live installer

Calamares is available only on a live ISO with a desktop. The builder installs
`calamares` and `calamares-settings-debian`, creates the desktop launcher and
adds the live-session polkit rule.

```bash
sudo python cli.py amd64 --distro debian-13 --desktop kde \
  --with-calamares --format iso --mode real
```

Validation rejects Calamares without a desktop, on VM/container output, or in
installer-only modes.

## Debian Installer

The builder downloads the official d-i kernel and initrd for the selected
suite and dpkg architecture from
`dists/<suite>/main/installer-<arch>/current/images/netboot`. It does not reuse
the live kernel. Installer-only netinstall and netboot builds also skip rootfs
bootstrap and package installation. Debian Installer output is limited to
Debian profiles.

Live ISO plus d-i:

```bash
sudo python cli.py amd64 --distro debian-13 --desktop xfce \
  --with-debian-installer --preseed xfce --mode real
```

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
