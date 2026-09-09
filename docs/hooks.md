# Build hooks

Build hooks use the existing three directories under `configs/hooks/`:

| Directory | Execution point | Environment |
|---|---|---|
| `pre-chroot` | Before toolchain setup and rootfs bootstrap | Host |
| `chroot` | After package installation, customization and bootloader setup, before unmounting | Target chroot |
| `post-chroot` | After artifact creation, before final checksums and workdir cleanup | Host |

Only executable, non-hidden, non-symlink `*.sh` files run, in alphabetical
order, through Bash. Hook sources are never created, rewritten or chmodded by
the executor. Missing phase directories are skipped without creating them.
Host hooks use the project root as their working directory. Target hooks run
with `/proc`, `/sys` and `/dev` mounted, using a unique temporary `.sh` file
inside target `/tmp`; each temporary copy is removed on success or failure.
Existing files with the source script name are preserved.

A non-zero hook exit stops the build. Resource unmounting and configured workdir
cleanup remain in the orchestrator's `finally` block; they do not depend on
user hooks. There are no separate error or cleanup hook directories.
Mock mode discovers hooks but never executes or stages them.

Use `--hooks-dir PATH` to select another root with the same three-directory
layout, or `--no-hooks` to disable all hooks. Relative roots are resolved from
the project root. The default is `hooks.directory = "configs/hooks"` in
`configs/global_build.json`; `hooks.enabled = false` also disables hooks.
The old `chroot_directory` setting is no longer part of the configuration.

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

The scripts already supplied in `configs/hooks` keep their own behavior. In
particular, the chroot cleanup script removes APT lists regardless of the
built-in cleaner's `cleanup.apt_lists` option. The post-chroot script lists
artifacts in the default output directory; it is not a checksum validator.
The orchestrator separately rejects missing or empty real-build artifacts.

The legacy `hooks/` tree is no longer discovered by the build pipeline. No
`<phase>.d` directories are required. Custom hooks for that layout must be
placed in the appropriate one of the three phases under the configured root.
