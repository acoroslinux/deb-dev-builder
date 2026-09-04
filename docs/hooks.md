# Build hooks

Host hooks are executable files placed in `hooks/<phase>.d/`; in-target hooks
are placed in `hooks/chroot/<phase>.d/`. Files run in lexical order. Host hooks
run directly without a shell wrapper; chroot hooks are copied into the target
and executed with `chroot`. A non-zero exit code stops the build. Mock mode
discovers hooks but never executes them.

| Phase | Runs |
|---|---|
| `pre-bootstrap` | before creating the target rootfs |
| `preflight` | before toolchain setup |
| `post-toolchain` | after toolchain setup |
| `post-bootstrap` | after bootstrap, APT sources and cache update |
| `pre-chroot-mount` | immediately before virtual filesystem mounts |
| `post-chroot-mount` | after virtual filesystem mounts |
| `pre-apt` | before writing APT sources and updating metadata |
| `post-apt` | after APT metadata update |
| `pre-packages` | immediately before package installation |
| `post-packages` | after package installation |
| `pre-customize` | before system customization |
| `post-customize` | after users, services, desktop and installer customization |
| `pre-installer` | before bootloader/installer staging |
| `post-installer` | after bootloader/installer staging |
| `pre-unmount` | immediately before chroot unmount |
| `post-unmount` | after chroot unmount |
| `pre-artifact` | after unmounting the target and before packaging |
| `post-artifact` | after artifact creation and before final checksums |
| `on-error` | when any build phase raises an error |
| `cleanup` | from the final cleanup path, on success or failure |

Available environment variables include `DEB_DEV_HOOK_PHASE`,
`DEB_DEV_HOOK_SCOPE`,
`DEB_DEV_WORKDIR`, `DEB_DEV_TARGET_ROOT`, `DEB_DEV_ARCH`,
`DEB_DEV_DPKG_ARCH`, `DEB_DEV_DISTRO`, `DEB_DEV_SUITE`,
`DEB_DEV_OUTPUT_FORMAT`, `DEB_DEV_ARTIFACT`, and `DEB_DEV_ERROR`.

Example:

```bash
mkdir -p hooks/post-customize.d
cp my-board-firmware-hook hooks/post-customize.d/20-board-firmware
chmod +x hooks/post-customize.d/20-board-firmware
sudo python cli.py --device pinebookpro --distro debian-13 --mode real
```

Use `--hooks-dir PATH` for another root or `--no-hooks` to disable the system.
The default is controlled by `hooks.enabled` and `hooks.directory` in
`configs/global_build.json`.

Installer-only netinstall/netboot builds skip bootstrap, APT and chroot phases;
their applicable hooks are `preflight`, `post-toolchain`, `pre-installer`,
`pre-artifact`, `post-installer`, `post-artifact`, `on-error` and `cleanup`.

Normal rootfs builds additionally run `pre-cleanup` and `post-cleanup` after
all target-root mounts are removed and before artifact packaging.
