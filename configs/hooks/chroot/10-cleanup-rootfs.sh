#!/bin/bash
set -euo pipefail

echo "=================================================="
echo "⚓ [CHROOT HOOK] Cleaning Up Package Caches & Temp Files"
echo "=================================================="

# Package manager cache cleanup
if command -v pacman >/dev/null 2>&1; then
    echo "  Cleaning pacman cache..."
    pacman -Scc --noconfirm 2>/dev/null || true
fi

if command -v dnf >/dev/null 2>&1; then
    echo "  Cleaning DNF cache..."
    dnf clean all 2>/dev/null || true
fi

if command -v zypper >/dev/null 2>&1; then
    echo "  Cleaning Zypper cache..."
    zypper clean -a 2>/dev/null || true
fi

if command -v apt-get >/dev/null 2>&1; then
    echo "  Cleaning APT cache..."
    apt-get clean 2>/dev/null || true
    rm -rf /var/lib/apt/lists/* 2>/dev/null || true
fi

if command -v xbps-remove >/dev/null 2>&1; then
    echo "  Cleaning XBPS cache..."
    xbps-remove -O -y 2>/dev/null || true
fi

if command -v emerge >/dev/null 2>&1; then
    echo "  Cleaning Portage distfiles & temp..."
    rm -rf /var/cache/distfiles/* 2>/dev/null || true
fi

# Clean temporary directories and system logs
echo "  Purging temporary directories..."
rm -rf /var/tmp/* /tmp/.* 2>/dev/null || true
find /tmp -mindepth 1 -not -name "*.sh" -delete 2>/dev/null || true

# Truncate log files to save SquashFS space
if [ -d /var/log ]; then
    find /var/log -type f -exec truncate -s 0 {} + 2>/dev/null || true
fi

echo "Rootfs cache cleanup complete."
