# Troubleshooting

## Read-only filesystem

The project root must be writable for default `workdir/`, `cache/`, and `output/`
paths. If the source checkout is read-only, provide a writable checkout or bind
mount it read-write. An explicit `--output` changes the final artifact location,
but real builds still need writable work and cache directories.

## APT or bootstrap failure

Confirm the selected distro profile has a valid `mirror`, `suite`, and non-empty
`components`. The builder now stops on package-install/download failures. For a
clean retry use `--clean --recreate-seed`; use `--no-clean` only with a known-good
rootfs.

## Stale mounts

Normal and exceptional exits unmount target/build-host filesystems. After a hard
kill, inspect `findmnt` and unmount only paths below this project's `workdir/`
before deleting it.

## Cleanup and retained workdirs

Real builds use `--clean` by default: the target architecture workdir is
removed before a fresh build and again after success or failure. Use
`--no-clean` to retain it for inspection or reuse a matching bootstrap marker.
The builder refuses output paths inside `workdir/`, because those artifacts
would otherwise be disposable staging data. A cleanup hook only removes its
own exact hook-staging directory; it cannot select an arbitrary tree.

## Cross architecture

The profile name remains `aarch64`, while Debian tools receive the `arm64` dpkg
architecture. The host still needs matching binfmt/QEMU support to execute target
binaries during a real cross-architecture build.
