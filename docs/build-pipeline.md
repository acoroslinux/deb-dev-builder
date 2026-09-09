# Build pipeline

1. Load and validate all selected JSON profiles.
2. Select native or isolated tools for the requested artifact format.
3. Run `preflight`/`post-toolchain` hooks and bootstrap with `mmdebstrap`, falling back to `debootstrap`. APT package recommendations remain enabled by default; only language downloads are disabled for efficiency.
4. Run bootstrap hooks, mount `/proc`, `/sys`, and recursive `/dev`, and run mount hooks.
5. Run APT hooks around source configuration and metadata update.
6. Run package hooks around installation; matching `hooks/chroot/<phase>.d` hooks run inside the target.
7. Apply users, services, desktop, installers, artwork, Flathub and ZRAM; run customization hooks.
8. Run installer hooks, unmount and verify target-root mounts, clean transient
   rootfs content, then run `pre-artifact` before packaging.
9. Run `post-artifact` hooks, then write checksums/manifests for the final bytes.
10. Run `on-error` after failures and `cleanup` on every exit before final mount cleanup.

APT failures are fatal. Debian security/update repositories are included only
when present in the distro profile; Devuan uses its merged repository and never
receives `systemd-sysv` unless an invalid configuration is explicitly attempted
(which validation rejects).
