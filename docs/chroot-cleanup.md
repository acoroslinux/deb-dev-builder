# Rootfs cleanup

Before packaging, the builder unmounts every mountpoint below the target root and verifies that no host filesystem remains mounted there. It then performs a conservative cleanup inspired by the `live-build` cleanup stages: APT lists and downloaded archives, logs, temporary files, user caches, Python bytecode and builder metadata are removed. Required directories and package databases are preserved.

The policy is controlled by the `cleanup` object in `configs/global_build.json`. Documentation, locales and package files are deliberately not removed automatically because doing so can break installed applications or accessibility.

The `pre-cleanup` and `post-cleanup` hook phases run after unmounting and before `pre-artifact`.
