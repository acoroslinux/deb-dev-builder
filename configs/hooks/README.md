# Build Hooks

Optional shell hook scripts are grouped into 3 canonical phases under `configs/hooks/`:

1. **`pre-chroot`**: Runs natively on the host at the beginning of the build process (before rootfs bootstrap/debootstrap).
2. **`chroot`**: Runs inside the chroot environment at the end of rootfs construction, after packages are installed and system configuration is complete.
3. **`post-chroot`**: Runs natively on the host in the binary phase after SquashFS and ISO/disk image generation.

## Execution Rules

- Only executable, non-symlink `*.sh` files are run, in alphabetical order.
- In-chroot hooks (`chroot/`) are copied to `/tmp/` inside the rootfs, executed within the isolated chroot, and cleaned up automatically.
- Environment variables provided:
  - `TARGET_ROOT`: Absolute path to target rootfs (on host).
  - `CHROOT_PATH`: Path to chroot rootfs.
  - `BUILD_ARCH`: Architecture being built (e.g. `amd64`).
  - `BUILD_DESKTOP`: Desktop environment profile (e.g. `xfce`).
  - `HOOK_PHASE`: Name of the active hook phase (`pre-chroot`, `chroot`, `post-chroot`).
