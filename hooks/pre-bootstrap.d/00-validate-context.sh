#!/bin/sh
set -eu

: "${DEB_DEV_WORKDIR:?missing build work directory}"
: "${DEB_DEV_TARGET_ROOT:?missing target root}"
: "${DEB_DEV_ARCH:?missing target architecture}"
: "${DEB_DEV_OUTPUT_FORMAT:?missing output format}"

mkdir -p "$DEB_DEV_WORKDIR"
