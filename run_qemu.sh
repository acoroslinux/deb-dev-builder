#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2023-2026 PeppermintOS Team

# ==============================================================================
# Void-Builder QEMU Runner Script
# Tests generated Void Linux ISOs and VM disk images (qcow2, img, raw, vdi, vmdk)
# under QEMU with BIOS/UEFI, KVM and custom graphics.
# ==============================================================================

set -e

# Default settings
DEFAULT_RAM="2048M"
DEFAULT_SMP="2"
DEFAULT_VGA="std"     # std is standard VGA (maximum compatibility)
DEFAULT_BIOS="auto"   # auto, bios or uefi
IMAGE_FILE=""
RAM="${DEFAULT_RAM}"
SMP="${DEFAULT_SMP}"
VGA="${DEFAULT_VGA}"
BOOT_MODE="${DEFAULT_BIOS}"
ENABLE_KVM=true

# Colors for terminal output
BOLD="\033[1m"
GREEN="\033[1;32m"
YELLOW="\033[1;33m"
BLUE="\033[1;34m"
RED="\033[1;31m"
RESET="\033[0m"

# Print banner
print_banner() {
    echo -e "${BLUE}==============================================================================${RESET}"
    echo -e "${BOLD} Void-Builder: QEMU ISO & Virtual Disk Test Runner${RESET}"
    echo -e "${BLUE}==============================================================================${RESET}"
}

# Print usage
usage() {
    print_banner
    echo -e "Usage: $0 [options] [path_to_image]"
    echo ""
    echo "Options:"
    echo "  -i, --iso, -d, --disk <file>  Specify ISO or VM disk path (defaults to newest in output/)"
    echo "  -m, --ram <size>              RAM size (default: 2048M, e.g. 4096M, 4G)"
    echo "  -c, --smp <cores>             Number of CPU cores (default: 2)"
    echo "  -v, --vga <type>              Graphics adapter: std (default/compatible), virtio, qxl, vmware"
    echo "  -u, --uefi                    Boot with UEFI (OVMF) instead of Legacy BIOS"
    echo "  -b, --bios                    Force Legacy BIOS boot mode"
    echo "  --no-kvm                      Disable KVM acceleration"
    echo "  -h, --help                    Show this help message"
    echo ""
    echo "Supported formats: .iso, .qcow2, .img, .raw, .vdi, .vmdk, .vhdx, .vhd"
    echo ""
    echo "Examples:"
    echo "  $0                                      # Boot newest image with Standard VGA"
    echo "  $0 output/void-x86_64.qcow2             # Boot QCOW2 virtual disk image"
    echo "  $0 -v virtio                            # Boot with VirtIO GPU driver"
    echo "  $0 -u                                   # Boot with UEFI"
    echo "  $0 -m 4G -c 4                           # Boot with 4GB RAM and 4 CPU cores"
    exit 0
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -i|--iso|-d|--disk|--image)
            IMAGE_FILE="$2"
            shift 2
            ;;
        -m|--ram)
            RAM="$2"
            shift 2
            ;;
        -c|--smp)
            SMP="$2"
            shift 2
            ;;
        -v|--vga)
            VGA="$2"
            shift 2
            ;;
        -u|--uefi)
            BOOT_MODE="uefi"
            shift
            ;;
        -b|--bios)
            BOOT_MODE="bios"
            shift
            ;;
        --no-kvm)
            ENABLE_KVM=false
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            if [[ -z "$IMAGE_FILE" && "$1" != -* ]]; then
                IMAGE_FILE="$1"
                shift
            else
                echo -e "${RED}Unknown option: $1${RESET}"
                usage
            fi
            ;;
    esac
done

print_banner

# Auto-detect image if not provided
if [[ -z "$IMAGE_FILE" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    OUTPUT_DIR="${SCRIPT_DIR}/output"

    if [[ -d "$OUTPUT_DIR" ]]; then
        IMAGE_FILE=$(ls -t "${OUTPUT_DIR}"/*.iso "${OUTPUT_DIR}"/*.qcow2 "${OUTPUT_DIR}"/*.img "${OUTPUT_DIR}"/*.raw "${OUTPUT_DIR}"/*.vdi "${OUTPUT_DIR}"/*.vmdk 2>/dev/null | head -n 1 || true)
    fi
fi

if [[ -z "$IMAGE_FILE" || ! -f "$IMAGE_FILE" ]]; then
    echo -e "${RED}Error: No ISO or VM disk image found or specified!${RESET}"
    echo "Please provide an image file path or compile one first."
    exit 1
fi

# Detect whether artifact is a disk image or a live ISO
IS_DISK_IMAGE=false
case "${IMAGE_FILE,,}" in
    *.qcow2|*.img|*.raw|*.vdi|*.vmdk|*.vhdx|*.vhd)
        IS_DISK_IMAGE=true
        ;;
esac

# Auto-select boot mode if set to 'auto'
if [[ "$BOOT_MODE" == "auto" ]]; then
    if [ "$IS_DISK_IMAGE" = true ]; then
        BOOT_MODE="uefi"
    else
        BOOT_MODE="bios"
    fi
fi

echo -e "${GREEN}Using Image:${RESET}  ${BOLD}${IMAGE_FILE}${RESET}"
echo -e "${GREEN}Image Type:${RESET}  $([ "$IS_DISK_IMAGE" = true ] && echo "Virtual Disk Image" || echo "Live CD/DVD ISO")"
echo -e "${GREEN}Memory:${RESET}      ${RAM}"
echo -e "${GREEN}CPU Cores:${RESET}   ${SMP}"
echo -e "${GREEN}Graphics:${RESET}    ${VGA}"
echo -e "${GREEN}Boot Mode:${RESET}   ${BOOT_MODE^^}"

# Check QEMU binary
QEMU_BIN="qemu-system-x86_64"
if ! command -v "$QEMU_BIN" >/dev/null 2>&1; then
    echo -e "${RED}Error: '$QEMU_BIN' is not installed on this system.${RESET}"
    exit 1
fi

# Build base QEMU command arguments
QEMU_ARGS=(
    "-m" "$RAM"
    "-smp" "$SMP"
    "-vga" "$VGA"
    "-display" "gtk"
    "-serial" "mon:stdio"
    "-net" "nic,model=virtio"
    "-net" "user"
    "-usb"
    "-device" "usb-tablet"
)

# Drive configuration (CD-ROM for ISO, VirtIO drive for disk images)
if [ "$IS_DISK_IMAGE" = true ]; then
    DRIVE_FORMAT="raw"
    case "${IMAGE_FILE,,}" in
        *.qcow2) DRIVE_FORMAT="qcow2" ;;
        *.vdi)   DRIVE_FORMAT="vdi" ;;
        *.vmdk)  DRIVE_FORMAT="vmdk" ;;
        *.vhdx)  DRIVE_FORMAT="vhdx" ;;
        *.vhd)   DRIVE_FORMAT="vpc" ;;
        *.img|*.raw) DRIVE_FORMAT="raw" ;;
    esac
    QEMU_ARGS+=(
        "-drive" "file=${IMAGE_FILE},if=virtio,format=${DRIVE_FORMAT},cache=writeback"
        "-boot" "c"
    )
else
    QEMU_ARGS+=(
        "-cdrom" "$IMAGE_FILE"
        "-boot" "d"
    )
fi

# KVM Acceleration check
if [ "$ENABLE_KVM" = true ]; then
    if [ -w /dev/kvm ] && [ -r /dev/kvm ]; then
        echo -e "${GREEN}KVM:${RESET}         ${BOLD}Enabled (hardware accelerated)${RESET}"
        QEMU_ARGS+=("-enable-kvm" "-cpu" "host")
    elif [ -e /dev/kvm ]; then
        echo -e "${YELLOW}KVM:${RESET}         /dev/kvm exists (run with sudo to enable full KVM speed)"
        QEMU_ARGS+=("-cpu" "max")
    else
        echo -e "${YELLOW}KVM:${RESET}         Disabled (/dev/kvm not found)"
        QEMU_ARGS+=("-cpu" "max")
    fi
else
    echo -e "${YELLOW}KVM:${RESET}         Disabled (by user flag)"
    QEMU_ARGS+=("-cpu" "max")
fi

# UEFI Configuration
if [ "$BOOT_MODE" = "uefi" ]; then
    OVMF_CODE=""
    OVMF_VARS=""

    # Candidate paths across Debian/Ubuntu, Arch, Fedora/RHEL, openSUSE
    OVMF_PAIRS=(
        "/usr/share/OVMF/OVMF_CODE_4M.fd:/usr/share/OVMF/OVMF_VARS_4M.fd"
        "/usr/share/OVMF/OVMF_CODE.fd:/usr/share/OVMF/OVMF_VARS.fd"
        "/usr/share/edk2/OvmfX64/OVMF_CODE.fd:/usr/share/edk2/OvmfX64/OVMF_VARS.fd"
        "/usr/share/edk2-ovmf/x64/OVMF_CODE.fd:/usr/share/edk2-ovmf/x64/OVMF_VARS.fd"
        "/usr/share/ovmf/x64/OVMF_CODE.fd:/usr/share/ovmf/x64/OVMF_VARS.fd"
    )

    for pair in "${OVMF_PAIRS[@]}"; do
        code="${pair%%:*}"
        vars="${pair##*:}"
        if [ -f "$code" ] && [ -f "$vars" ]; then
            OVMF_CODE="$code"
            OVMF_VARS="$vars"
            break
        fi
    done

    if [ -n "$OVMF_CODE" ] && [ -n "$OVMF_VARS" ]; then
        TMP_VARS=$(mktemp --suffix=_VARS.fd)
        cp "$OVMF_VARS" "$TMP_VARS"
        trap 'rm -f "$TMP_VARS"' EXIT

        QEMU_ARGS+=(
            "-drive" "if=pflash,format=raw,readonly=on,file=$OVMF_CODE"
            "-drive" "if=pflash,format=raw,file=$TMP_VARS"
        )
    elif [ -f "/usr/share/ovmf/OVMF.fd" ]; then
        QEMU_ARGS+=("-bios" "/usr/share/ovmf/OVMF.fd")
    elif [ -f "/usr/share/qemu/OVMF.fd" ]; then
        QEMU_ARGS+=("-bios" "/usr/share/qemu/OVMF.fd")
    else
        echo -e "${RED}Error: OVMF UEFI firmware files not found on host.${RESET}"
        echo "Falling back to BIOS..."
    fi
fi

echo -e "${BLUE}------------------------------------------------------------------------------${RESET}"
echo -e "${BOLD}Launching QEMU (Serial console attached to this terminal)...${RESET}"
echo -e "${BLUE}------------------------------------------------------------------------------${RESET}"

# Execute QEMU
exec "$QEMU_BIN" "${QEMU_ARGS[@]}"
