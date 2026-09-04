#!/bin/sh
set -eu

: "${DEB_DEV_ARTIFACT:?missing artifact path}"
test -s "$DEB_DEV_ARTIFACT"
