# Rootfs cleanup

Before packaging, the builder unmounts every mountpoint below the target root and verifies that no host filesystem remains mounted there. It then performs a conservative cleanup inspired by the `live-build` cleanup stages: APT lists and downloaded archives, logs, temporary files, user caches, Python bytecode and builder metadata are removed. Required directories and package databases are preserved.

The policy is controlled by the `cleanup` object in `configs/global_build.json`. Documentation, locales and package files are deliberately not removed automatically because doing so can break installed applications or accessibility.

The `chroot` hook phase runs before unmounting and cleanup. `post-chroot` runs
after artifact creation. Resource cleanup also runs on failure, independently
of user hooks. Use `--no-rootfs-cleanup` to skip transient rootfs file removal;
resource unmounting still takes place. See [hooks](hooks.md).
