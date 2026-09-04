#!/bin/sh
set -eu

if [ -n "${DEB_DEV_TARGET_ROOT:-}" ]; then
    rm -rf -- "$DEB_DEV_TARGET_ROOT/run/deb-dev-builder-hooks"
fi
