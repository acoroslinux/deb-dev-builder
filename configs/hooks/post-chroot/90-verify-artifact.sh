#!/bin/bash
set -euo pipefail

# The orchestrator supplies the artifact from this build, including custom paths.
artifact="${DEB_DEV_ARTIFACT:?missing artifact path}"
if [ ! -f "$artifact" ] || [ ! -s "$artifact" ]; then
    echo "Artifact missing or empty: $artifact" >&2
    exit 1
fi

printf 'Artifact verified: %s\n' "$artifact"
# Final checksums and the package manifest are generated after this phase.
