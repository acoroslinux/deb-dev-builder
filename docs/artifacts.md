# Artifact formats

| CLI format | Result | Validation performed |
|---|---|---|
| `iso` | Hybrid/UEFI `.iso` | kernel/initramfs required; `xorriso` must succeed and create a non-empty file |
| `netboot` | d-i `.netboot.tar.gz` PXE tree | official archive must download, extract safely, and repack successfully |
| `img` | GPT raw `.img` | GPT/ESP/root assembly commands must succeed; file must be non-empty |
| `raw` | GPT raw `.raw` | same structural checks as IMG |
| `qcow2` | QEMU QCOW2 | `qemu-img convert` and `qemu-img info` |
| `vdi` | VirtualBox VDI | `qemu-img convert` and `qemu-img info` |
| `vmdk` | VMware VMDK | `qemu-img convert` and `qemu-img info` |
| `vhd` / `vhdx` | Hyper-V compatible disk | `qemu-img convert` and `qemu-img info` |
| `tarball` | `.tar.xz` rootfs | tar exits successfully; virtual filesystems are excluded |
| `oci` / `container` | `.oci.tar` | OCI layout, index, manifest, config, layer, and SHA-256 content-addressed blobs |

Disk images are left in their native format unless `--compression` is supplied.
Requested compression (`zstd`, `xz`, `gzip`, or `lz4`) happens after native
validation. Checksums describe the final artifact. The OCI output is an OCI
image-layout archive, not a plain rootfs tar.

Disk output generates removable UEFI files without probing the host root
filesystem. On x86, `grub2-bios` and `grub2-hybrid` also create a GPT BIOS boot
partition and install GRUB against the image's root partition using a temporary
loop device. The mount and loop device are released on success or failure.
BIOS installation requires loop-device access and mount privileges.
The `syslinux` profile supports x86 disks with ext4, using extlinux and GPT MBR
boot code with the root partition marked for legacy boot.

OCI layers are hashed from disk in chunks, keeping memory use independent of
rootfs size. Runtime directories such as `/tmp` are preserved empty with their
permissions; their build-time contents are excluded. ARM hard-float images
declare the OCI `arm/v7` platform.

Use `--no-manifest` to disable checksum and package-manifest generation.

VM profiles select both the native format and guest integration packages:

| Profile | Format | Guest integration |
|---|---|---|
| `qemu` | QCOW2 | `qemu-guest-agent` |
| `virtualbox` | VDI | kernel-native drivers; no unavailable external repository package |
| `vmware` | VMDK | `open-vm-tools` |
| `hyperv` | VHDX | `hyperv-daemons` |

Examples:

```bash
sudo python cli.py amd64 --distro debian-12 --format qcow2 --output vm/debian.qcow2 --mode real
sudo python cli.py amd64 --distro debian-13 --vm-profile qemu --mode real
sudo python cli.py amd64 --distro devuan-5 --format oci --output containers/devuan --mode real
```
