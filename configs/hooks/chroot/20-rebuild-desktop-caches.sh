#!/bin/bash
set -euo pipefail

echo "=================================================="
echo "⚓ [CHROOT HOOK] Rebuilding Desktop & Font Caches"
echo "=================================================="

# Update desktop application database
if command -v update-desktop-database >/dev/null 2>&1 && [ -d /usr/share/applications ]; then
    echo "  Updating desktop database..."
    update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi

# Update GTK icon caches
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    echo "  Updating GTK icon caches..."
    for theme in /usr/share/icons/*; do
        if [ -d "$theme" ] && [ -f "$theme/index.theme" ]; then
            gtk-update-icon-cache -f -q "$theme" >/dev/null 2>&1 || true
        fi
    done
fi

# Update font caches
if command -v fc-cache >/dev/null 2>&1; then
    echo "  Updating fontconfig caches..."
    fc-cache -f >/dev/null 2>&1 || true
fi

# Compile GLib schemas
if command -v glib-compile-schemas >/dev/null 2>&1 && [ -d /usr/share/glib-2.0/schemas ]; then
    echo "  Compiling GLib schemas..."
    glib-compile-schemas /usr/share/glib-2.0/schemas >/dev/null 2>&1 || true
fi

# Update MIME database
if command -v update-mime-database >/dev/null 2>&1 && [ -d /usr/share/mime ]; then
    echo "  Updating MIME database..."
    update-mime-database /usr/share/mime >/dev/null 2>&1 || true
fi

echo "Desktop & font caches rebuilt successfully."
