#!/bin/sh
# Debian Installer (d-i) Late Command Hook Script
# Executed in the installer environment; /target is the installed system.
# Installer overlays and generated scripts are selected in customization.json.

# Refresh schemas already installed by the selected packages.
if [ -d /target/usr/share/glib-2.0/schemas ]; then
    chroot /target glib-compile-schemas /usr/share/glib-2.0/schemas 2>/dev/null || true
fi

exit 0
