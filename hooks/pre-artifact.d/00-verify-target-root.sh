#!/bin/sh
set -eu

: "${DEB_DEV_TARGET_ROOT:?missing target root}"
test -d "$DEB_DEV_TARGET_ROOT"
test -d "$DEB_DEV_TARGET_ROOT/etc"
test -d "$DEB_DEV_TARGET_ROOT/usr"
