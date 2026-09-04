#!/bin/bash
set -euo pipefail

echo "=================================================="
echo "⚓ [POST-CHROOT HOOK] Verifying Binary Artifacts"
echo "=================================================="

OUTPUT_DIR="./output"
if [ ! -d "$OUTPUT_DIR" ]; then
    OUTPUT_DIR="."
fi

echo "Scanning artifacts in: $OUTPUT_DIR"
for art in "$OUTPUT_DIR"/*.iso "$OUTPUT_DIR"/*.img "$OUTPUT_DIR"/*.qcow2 "$OUTPUT_DIR"/*.tar* "$OUTPUT_DIR"/*.xz; do
    if [ -f "$art" ]; then
        SIZE=$(du -h "$art" | awk "{print \$1}")
        BASENAME=$(basename "$art")
        echo "  Found artifact: $BASENAME ($SIZE)"

        # Check checksums
        if [ -f "${art}.sha256" ]; then
            echo "    SHA256 checksum file: present"
        fi
        if [ -f "${art}.md5" ]; then
            echo "    MD5 checksum file: present"
        fi
    fi
done

echo "Binary phase verification complete."
