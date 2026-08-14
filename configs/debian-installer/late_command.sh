#!/bin/sh
# Debian Installer (d-i) Late Command Hook Script
# Executed inside /target right after system installation finishes.
# Use this script to customize the target system, copy branding, configure services, or run post-install tweaks.

echo "🚀 Executing Debian-Installer Late Command Hook inside /target..."

# 1. Ensure sudoers configuration for default users
if [ -d /target/etc/sudoers.d ]; then
    echo "liveuser ALL=(ALL) NOPASSWD: ALL" > /target/etc/sudoers.d/liveuser_nopasswd
    echo "admin ALL=(ALL) NOPASSWD: ALL" > /target/etc/sudoers.d/admin_nopasswd
    chmod 0440 /target/etc/sudoers.d/*_nopasswd
fi

# 2. Copy Custom Files / Desktop Customizations from ISO into Target System if present on CD-ROM
if [ -d /cdrom/configs/custom_files ]; then
    echo "🎨 Copying desktop customizations from /cdrom/configs/custom_files to /target..."
    cp -a /cdrom/configs/custom_files/* /target/ 2>/dev/null || true
fi

# 3. Re-compile GSettings schemas inside target system if schemas were copied
if [ -d /target/usr/share/glib-2.0/schemas ]; then
    chroot /target glib-compile-schemas /usr/share/glib-2.0/schemas 2>/dev/null || true
fi

exit 0
