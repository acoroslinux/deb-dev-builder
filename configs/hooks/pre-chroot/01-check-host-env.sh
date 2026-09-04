#!/bin/bash
set -euo pipefail

echo "=================================================="
echo "⚓ [PRE-CHROOT HOOK] Validating Host Build Environment"
echo "=================================================="
echo "  Build Arch:     ${BUILD_ARCH:-unknown}"
echo "  Build Desktop:  ${BUILD_DESKTOP:-none}"
echo "  Target Root:    ${TARGET_ROOT:-none}"

# Check available disk space (at least 5GB recommended)
if command -v df >/dev/null 2>&1; then
    AVAIL_KB=$(df -k . | awk "NR==2 {print \$4}")
    AVAIL_MB=$((AVAIL_KB / 1024))
    echo "  Available Disk: ${AVAIL_MB} MB"
    if [ "$AVAIL_MB" -lt 5120 ]; then
        echo "  ⚠️ Warning: Less than 5GB free disk space available (${AVAIL_MB} MB)."
    fi
fi

# Check essential host packaging tools
for tool in mksquashfs xorriso tar sha256sum; do
    if command -v "$tool" >/dev/null 2>&1; then
        echo "  Tool check [OK]: $tool"
    else
        echo "  Tool check [MISSING]: $tool (may be required depending on output format)"
    fi
done

echo "Host environment validation complete."
