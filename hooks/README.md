# Build hooks

Place executable host files in one of these directories. Files run in lexical order:

- `preflight.d`
- `post-toolchain.d`
- `pre-bootstrap.d`
- `post-bootstrap.d`
- `pre-chroot-mount.d`
- `post-chroot-mount.d`
- `pre-apt.d`
- `post-apt.d`
- `pre-packages.d`
- `post-packages.d`
- `pre-customize.d`
- `post-customize.d`
- `pre-installer.d`
- `post-installer.d`
- `pre-unmount.d`
- `post-unmount.d`
- `pre-cleanup.d`
- `post-cleanup.d`
- `pre-artifact.d`
- `post-artifact.d`
- `on-error.d`
- `cleanup.d`

Hooks run on the build host without a shell wrapper and inherit `DEB_DEV_*`
environment variables describing the build, work directory, target root,
artifact and current phase. They are simulated but not executed in mock mode.

For hooks that must run inside the image, use the same phase names below
`hooks/chroot/`, for example `hooks/chroot/post-packages.d/10-enable-service`.

The repository includes a small generic default set: context validation before
bootstrap, target-root validation before packaging, final-artifact validation,
temporary hook staging cleanup, and removal of generated machine/host identity
files from image builds. These hooks do not mention or depend on a particular
distribution and can be removed or overridden when a different image policy is
required.

The default cleanup hook only removes
`$DEB_DEV_TARGET_ROOT/run/deb-dev-builder-hooks`, the exact temporary directory
used for in-target hooks. It does not remove the rootfs or the workdir.

Rootfs cleanup runs between `post-unmount` and `pre-artifact`; see
`docs/chroot-cleanup.md` for its conservative policy and configuration flags.
