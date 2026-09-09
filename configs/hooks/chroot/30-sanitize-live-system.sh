#!/bin/bash
set -euo pipefail

echo "=================================================="
echo "⚓ [CHROOT HOOK] Sanitizing Live System Identity"
echo "=================================================="

# Machine ID: truncate /etc/machine-id to initialize a fresh ID on first live boot
if [ -f /etc/machine-id ]; then
    echo "  Resetting /etc/machine-id for fresh live boot generation..."
    truncate -s 0 /etc/machine-id
fi

# DBus machine-id
if [ -f /var/lib/dbus/machine-id ] && [ ! -L /var/lib/dbus/machine-id ]; then
    rm -f /var/lib/dbus/machine-id
fi

# Remove persistent udev network rules so interface naming is dynamic
for udev_rule in /etc/udev/rules.d/70-persistent-net.rules /etc/udev/rules.d/70-persistent-cd.rules; do
    if [ -f "$udev_rule" ]; then
        echo "  Removing static udev rule: $(basename "$udev_rule")"
        rm -f "$udev_rule"
    fi
done

# Clear shell histories in user homes and /root
rm -f /root/.bash_history /root/.zsh_history 2>/dev/null || true
if [ -d /home ]; then
    find /home -maxdepth 2 -name ".*history" -delete 2>/dev/null || true
fi

echo "System sanitization complete."
