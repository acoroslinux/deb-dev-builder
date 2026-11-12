# Build hooks

Build hooks live only under `configs/hooks/`, in three flat directories:

| Directory | Execution point | Environment |
|---|---|---|
| `pre-chroot` | Before toolchain setup and rootfs bootstrap | Host |
| `chroot` | After package installation and rootfs customization, before unmounting and packaging | Target chroot |
| `post-chroot` | After artifact creation, before final checksums and workdir cleanup | Host |

Only executable, non-hidden, non-symlink `*.sh` files run, in alphabetical
order, through Bash. Hook sources are never created, rewritten or chmodded by
the executor. Missing phase directories are skipped without creating them.
Host hooks use the project root as their working directory. Target hooks run
with `/proc`, `/sys` and `/dev` mounted, using a unique temporary `.sh` file
inside target `/tmp`; each temporary copy is removed on success or failure.
Existing files with the source script name are preserved. Unknown phase
directories and nested hook directories are rejected before any hook runs;
put numbered `.sh` files directly inside the appropriate phase.

A non-zero hook exit stops the build. Resource unmounting and configured workdir
cleanup remain in the orchestrator's `finally` block; they do not depend on
user hooks. There are no separate error or cleanup hook directories.
Mock mode discovers hooks but never executes or stages them.

Use `--hooks-dir PATH` to select another root with the same three-directory
layout, or `--no-hooks` to disable all hooks. Relative roots are resolved from
the project root. The default is `hooks.directory = "configs/hooks"` in
`configs/global_build.json`; `hooks.enabled = false` also disables hooks.

All three phases receive `TARGET_ROOT`, `CHROOT_PATH`, `BUILD_ARCH`,
`BUILD_DESKTOP` and `HOOK_PHASE`. Root paths describe the absolute target
location on the host; scripts running inside the chroot use `/` to access the
target filesystem. Additional context is available as `DEB_DEV_WORKDIR`,
`DEB_DEV_TARGET_ROOT`, `DEB_DEV_ARCH`, `DEB_DEV_DPKG_ARCH`, `DEB_DEV_DISTRO`,
`DEB_DEV_SUITE`, `DEB_DEV_OUTPUT_FORMAT`, `DEB_DEV_HOOK_PHASE`,
`DEB_DEV_HOOK_SCOPE` and `DEB_DEV_ARTIFACT` (empty before artifact creation).

Installer-only netinstall and netboot builds run the two host phases and skip
`chroot`, because they do not construct a target rootfs. Other artifact formats
run all three phases. Final checksums are generated after `post-chroot` so
changes made by a hook are included.

The bundled chroot scripts clean package caches, rebuild desktop caches and
sanitize machine identity, SSH host keys and shell histories. The cleanup script
removes APT lists regardless of the built-in cleaner's `cleanup.apt_lists` option.
The post-chroot script verifies only `DEB_DEV_ARTIFACT`, the artifact produced by
the current build, including custom output paths. Checksums are generated after
this phase; the hook does not scan old artifacts from previous builds.

To add a hook, create (for example) `configs/hooks/chroot/40-my-customization.sh`
and make it executable with `chmod +x`. There is no second hook root in the
repository and no extra phase directories to configure.
