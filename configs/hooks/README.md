# Build hooks

All project hooks live here, in exactly three phases:

```text
configs/hooks/
├── pre-chroot/   # Host: before preparing the toolchain and rootfs
├── chroot/       # Target: after packages and customization, before packaging
└── post-chroot/  # Host: after creating the artifact, before final checksums
```

Place executable `*.sh` scripts directly inside the relevant directory. They
run in filename order, so use numeric prefixes such as `10-`, `20-`, `30-`.
Nested directories and unknown phases are rejected. Hidden files, non-executable
files and symlinks to scripts are not run. A failing script stops the build.

For example, add `chroot/40-my-customization.sh` and run:

```bash
chmod +x configs/hooks/chroot/40-my-customization.sh
```

Host hooks run from the project root. Chroot scripts run against `/`, with
`/proc`, `/sys` and `/dev` mounted. The executor stages a temporary copy under
target `/tmp` and removes it afterwards; source scripts are never modified.

Common environment variables:

- `TARGET_ROOT`, `CHROOT_PATH`: target rootfs path on the host. Inside chroot use `/`.
- `BUILD_ARCH`, `BUILD_DESKTOP`: selected architecture and desktop.
- `HOOK_PHASE`: `pre-chroot`, `chroot` or `post-chroot`.
- `DEB_DEV_ARTIFACT`: current artifact's absolute path in `post-chroot`.

The bundled scripts check the host environment, clean/rebuild caches, sanitize
machine identity and SSH host keys, and verify the current artifact. Resource
unmounting and build-directory cleanup are handled by the builder even on failure.

Settings live under `hooks` in `../global_build.json`. `--hooks-dir PATH` selects
an alternative root with the same three-phase layout; `--no-hooks` disables hooks.
Mock mode does not execute scripts. See [the manual](../../docs/hooks.md) for the
full execution context and installer-only builds.
