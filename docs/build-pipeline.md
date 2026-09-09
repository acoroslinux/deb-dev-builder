# Build pipeline

1. Load and validate the selected JSON profiles.
2. Run `configs/hooks/pre-chroot` on the host, then prepare the isolated toolchain.
3. Bootstrap with `mmdebstrap`, falling back to `debootstrap`. APT recommendations remain enabled by default.
4. Mount the target's `/proc`, `/sys` and `/dev`, configure APT sources and update metadata.
5. Install packages and prepare any requested offline repository.
6. Configure users, services, desktop, installers, artwork, Flathub, ZRAM and bootloaders.
7. Run `configs/hooks/chroot` inside the mounted target.
8. Unmount and verify the target mounts, clean transient rootfs files and create the artifact.
9. Run `configs/hooks/post-chroot` on the host, verify the artifact and generate final checksums/manifests.
10. Always attempt to unmount remaining target/build-host resources and perform configured workdir cleanup, including on failure.

Installer-only netinstall/netboot builds skip rootfs construction and the
chroot hook phase. Mock mode does not execute any hook scripts.
