#!/bin/sh
set -eu

if [ -n "${DEB_DEV_TARGET_ROOT:-}" ]; then
    if [ -d "$DEB_DEV_TARGET_ROOT/run/deb-dev-builder-hooks" ]; then
        rm -rf -- "$DEB_DEV_TARGET_ROOT/run/deb-dev-builder-hooks"
    fi
fi
