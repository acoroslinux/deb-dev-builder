#!/usr/bin/env bash
set -euo pipefail

# Create the portable build_host used by --force-isolated-toolchain.
# Run this on a Debian Trixie staging machine with mmdebstrap/debootstrap.
OUTPUT=${1:-build-host-amd64.tar.zst}
ARCH=${BUILD_HOST_ARCH:-$(dpkg --print-architecture)}
SUITE=${BUILD_HOST_SUITE:-trixie}
MIRROR=${BUILD_HOST_MIRROR:-https://deb.debian.org/debian}
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (the output is only a tarball; it is not installed on the host)." >&2
  exit 1
fi
if ! command -v mmdebstrap >/dev/null 2>&1 && ! command -v debootstrap >/dev/null 2>&1; then
  echo "mmdebstrap or debootstrap is required on the staging machine." >&2
  exit 1
fi

PACKAGES="mmdebstrap,debootstrap,squashfs-tools,zstd,xorriso,grub-common,grub-pc-bin,grub-efi-amd64-bin,grub-efi-ia32-bin,mtools,dosfstools,qemu-utils,parted,btrfs-progs,syslinux-utils,fdisk,util-linux,ca-certificates,xfsprogs,f2fs-tools,xz-utils,gzip,lz4"
if command -v mmdebstrap >/dev/null 2>&1; then
  mmdebstrap --arch="$ARCH" --variant=essential --include="$PACKAGES" "$SUITE" "$STAGE" "$MIRROR"
else
  debootstrap --arch="$ARCH" --include="$PACKAGES" "$SUITE" "$STAGE" "$MIRROR"
fi

install -d -m 0755 "$(dirname "$OUTPUT")"
tar --zstd --sort=name --mtime='UTC 1970-01-01' --numeric-owner \
  --owner=0 --group=0 -C "$STAGE" -cf "$OUTPUT" .
sha256sum "$OUTPUT" | tee "$OUTPUT.sha256"
cat > "$OUTPUT.manifest" <<EOF
suite=$SUITE
architecture=$ARCH
created_by=deb-dev-builder/tools/create-build-host-tarball.sh
archive=$(basename "$OUTPUT")
sha256=$(sha256sum "$OUTPUT" | awk '{print \$1}')
EOF
echo "Created $OUTPUT"
