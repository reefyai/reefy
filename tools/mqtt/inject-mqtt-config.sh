#!/bin/bash
#
# Inject MQTT Configuration into Reefy Raw Disk Image
#
# This script injects MQTT configuration (certificates and mqtt.conf) into
# a raw Reefy disk image. Run this after every image rebuild to include
# MQTT configuration in the image.
#
# Usage:
#   sudo ./inject-mqtt-config.sh [OPTIONS]
#
# Options:
#   -o, --output DIR        Output directory from setup-mqtt-server.sh (default: ./mqtt-server)
#   -i, --image FILE        Raw disk image to inject into (required)
#   -h, --help              Show this help message
#
# The script looks for the USB bundle at <output>/usb-bundle/mqtt/
#
# Examples:
#   # Inject using default output directory
#   sudo ./inject-mqtt-config.sh -i ../../buildroot/output/images/reefy.raw
#
#   # Inject using custom output directory
#   sudo ./inject-mqtt-config.sh -o ./mqtt-prod -i /path/to/reefy.raw
#

set -euo pipefail

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# Defaults
OUTPUT_DIR="./mqtt-server"
IMAGE_FILE=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        -i|--image)
            IMAGE_FILE="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Resolve bundle directory from output dir
BUNDLE_DIR="${OUTPUT_DIR}/usb-bundle/mqtt"

# Validate arguments
if [[ -z "${IMAGE_FILE}" ]]; then
    log_error "Image file is required (-i/--image)"
    echo ""
    echo "Usage: sudo $0 -i /path/to/reefy.raw [-o ./mqtt-server]"
    exit 1
fi

if [[ ! -d "${BUNDLE_DIR}" ]]; then
    log_error "USB bundle not found: ${BUNDLE_DIR}"
    log_error "Run setup-mqtt-server.sh first, or use -o to specify the output directory"
    exit 1
fi

if [[ ! -f "${IMAGE_FILE}" ]]; then
    log_error "Image file not found: ${IMAGE_FILE}"
    exit 1
fi

# Verify bundle contents
required_files=("ca.crt" "bootstrap.crt" "bootstrap.key" "mqtt.conf")
for f in "${required_files[@]}"; do
    if [[ ! -f "${BUNDLE_DIR}/${f}" ]]; then
        log_error "Missing required file in bundle: ${BUNDLE_DIR}/${f}"
        log_error "Run setup-mqtt-server.sh first to generate the USB bundle"
        exit 1
    fi
done

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    log_error "Root privileges required to mount image"
    log_info "Run with: sudo $0 -i ${IMAGE_FILE} -o ${OUTPUT_DIR}"
    exit 1
fi

log_info "Reefy MQTT Config Injection"
log_info "=========================="
log_info "Bundle:  ${BUNDLE_DIR}"
log_info "Image:   ${IMAGE_FILE}"
log_info ""

# Create temporary mount point
tmp_mount=$(mktemp -d)
trap "umount ${tmp_mount} 2>/dev/null || true; rm -rf ${tmp_mount}" EXIT

# Set up loop device with partition scanning
log_info "Setting up loop device..."
loop_dev=$(losetup --show -f -P "${IMAGE_FILE}")

if [[ -z "${loop_dev}" ]]; then
    log_error "Failed to create loop device"
    exit 1
fi

trap "losetup -d ${loop_dev} 2>/dev/null || true; umount ${tmp_mount} 2>/dev/null || true; rm -rf ${tmp_mount}" EXIT

# Wait for partition to appear
sleep 1
partprobe "${loop_dev}" 2>/dev/null || true
sleep 1

# Mount the first partition (ESP/vfat)
part_dev="${loop_dev}p1"

if [[ ! -b "${part_dev}" ]]; then
    log_error "Partition not found: ${part_dev}"
    log_error "Is this a valid Reefy raw image with a GPT partition table?"
    exit 1
fi

log_info "Mounting partition ${part_dev}..."
mount "${part_dev}" "${tmp_mount}"

# Create mqtt directory and copy files
log_info "Copying MQTT configuration to image..."
mkdir -p "${tmp_mount}/mqtt"
cp -r "${BUNDLE_DIR}"/* "${tmp_mount}/mqtt/"

# Verify
log_info "Files injected into image:"
ls -lh "${tmp_mount}/mqtt/" | tail -n +2

# Cleanup
log_info "Unmounting..."
umount "${tmp_mount}"
losetup -d "${loop_dev}"
rm -rf "${tmp_mount}"

trap - EXIT

log_info ""
log_info "MQTT config successfully injected into ${IMAGE_FILE}"
log_info ""
log_info "Next steps:"
log_info "  1. Boot the image (e.g., with reefy-local-boot.sh)"
log_info "  2. The device will auto-detect MQTT config and start reefy-mqtt service"
