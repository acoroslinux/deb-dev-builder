Deb-Dev-Builder
===============

Deb-Dev-Builder creates Debian and Devuan live media, virtual-machine disks,
root filesystem archives, and OCI image-layout archives from composable JSON
profiles. Paths are resolved independently of the shell's current directory.

.. toctree::
   :maxdepth: 2
   :caption: Manual

   getting-started
   configuration
   build-pipeline
   artifacts
   installers
   hooks
   chroot-cleanup
   toolchain
   troubleshooting
   development

Safety model
------------

Real builds require root for bootstrap, mounts, and bootable disk assembly.
Use ``--mode mock`` to validate profile composition and output routing without
changing the host. Real builds always unmount chroot and isolated-toolchain
filesystems in a cleanup block, including after failures. Destructive workdir
cleanup is constrained to the project workdir and never follows symlinks.
