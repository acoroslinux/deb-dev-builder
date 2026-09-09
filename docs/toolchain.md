# Portable build toolchain

Real builds always use a separate `build_host` rootfs and never execute build
tools directly from the host. The active build host lives under `workdir/` and
the reusable archive lives under `cache/toolchains/`. APT archives and
verified rootfs seeds live under `cache/<architecture>/`.

When no verified build-host archive is cached, the default `isolated-oci`
backend downloads the official Debian OCI base image to `cache/oci/`,
verifies every manifest and layer digest, and installs build dependencies only
inside the isolated rootfs. It then caches the prepared build host for reuse.

The same Debian Trixie build host is used for Debian and Devuan targets. Before
a Devuan bootstrap, the builder installs a pinned and SHA-256-verified
`devuan-keyring` package inside that build host; it never enables unauthenticated
APT repositories. The pinned package and its checksum are versioned in
`tools/keyrings/` and are preferred over a network download. Cross-architecture target customization requires an available
QEMU/binfmt handler for the target architecture.

Debian's debootstrap includes the generic `ceres` Devuan script. The builder
creates local `daedalus`, `excalibur`, and `freia` aliases to it when required,
so maintained Devuan profiles work even when Debian does not ship their aliases.

Generate a portable tarball on a Debian Trixie staging machine with:

```bash
sudo tools/create-build-host-tarball.sh cache/toolchains/build-host-amd64.tar.zst
```

Configure its path and checksum in `configs/global_build.json` under
`toolchain.tarball_path` and `toolchain.tarball_sha256`, or use the equivalent
`DEB_DEV_BUILDER_TOOLCHAIN_TARBALL` and
`DEB_DEV_BUILDER_TOOLCHAIN_SHA256` environment variables. The builder verifies
the checksum before extraction and never installs the toolchain into the host.
The SHA-256 digest is mandatory for every local, cached, or downloaded tarball.
After a bootstrap-built host is prepared, the builder writes an updated,
verified `.tar.xz` archive and checksum to its workspace cache for later runs.
Cache compression uses `xz --threads=0`, automatically consuming the available
CPU cores while retaining the XZ integrity check.
The default OCI reference is Debian stable (`trixie`); pin a digest in local
configuration when a fully immutable bootstrap source is required.
