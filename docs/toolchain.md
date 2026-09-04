# Portable build toolchain

Real builds always use a separate `build_host` rootfs and never execute build
tools directly from the host. The active build host lives under `workdir/` and
the reusable archive lives under `workdir/cache/toolchains/`. APT archives and
verified rootfs seeds live under `workdir/cache/<architecture>/`.

When no verified build-host archive is cached, the default `isolated-oci`
backend downloads the official Debian OCI base image to `workdir/cache/oci/`,
verifies every manifest and layer digest, and installs build dependencies only
inside the isolated rootfs. It then caches the prepared build host for reuse.

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
The default OCI reference is Debian stable (`trixie`); pin a digest in local
configuration when a fully immutable bootstrap source is required.
