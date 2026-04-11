#!/bin/bash
#
# Send mcl Config via MQTT
#
# Packages mcl config files as a base64-encoded tar.gz bundle and publishes
# to a device's command topic over MQTT. The device extracts the config and
# (re)starts the mgmt daemon for continuous drift monitoring.
#
# Usage:
#   ./send-mcl.sh -c CERTS_DIR -d UUID MCL_PATH
#
# Options:
#   -c, --certs DIR         Certificate directory (required)
#   -d, --device UUID       Target device UUID (required)
#   -H, --host HOST         MQTT broker host (default: localhost)
#   -p, --port PORT         MQTT broker port (default: 8883)
#   -v, --version VER       Version label (default: timestamp)
#   -w, --watch             Watch for device status response
#   -h, --help              Show this help message
#
# The certs directory must contain:
#   - ca.crt                CA certificate
#   - device cert+key with CN=UUID (from device-certs/{UUID}/)
#     OR bootstrap.crt/bootstrap.key for testing
#
# Examples:
#   # Send a single mcl file (auto-renamed to main.mcl on device)
#   ./send-mcl.sh -c ./mqtt-server/certs -d UUID test-config.mcl
#
#   # Send a directory of mcl files (must contain main.mcl)
#   ./send-mcl.sh -c ./mqtt-server/certs -d UUID ./my-config/
#
#   # Send and watch for result
#   ./send-mcl.sh -c ./mqtt-server/certs -d UUID -w test-config.mcl
#

set -euo pipefail

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }

# Defaults
CERTS_DIR=""
DEVICE_UUID=""
HOST="localhost"
PORT="8883"
VERSION=""
WATCH=false
MCL_PATH=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -c|--certs)
            CERTS_DIR="$2"
            shift 2
            ;;
        -d|--device)
            DEVICE_UUID="$2"
            shift 2
            ;;
        -H|--host)
            HOST="$2"
            shift 2
            ;;
        -p|--port)
            PORT="$2"
            shift 2
            ;;
        -v|--version)
            VERSION="$2"
            shift 2
            ;;
        -w|--watch)
            WATCH=true
            shift
            ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        -*)
            log_error "Unknown option: $1"
            echo "Use --help for usage information" >&2
            exit 1
            ;;
        *)
            MCL_PATH="$1"
            shift
            ;;
    esac
done

# Validate
if [[ -z "${CERTS_DIR}" ]]; then
    log_error "Certificate directory required (-c/--certs)"
    exit 1
fi

if [[ -z "${DEVICE_UUID}" ]]; then
    log_error "Device UUID required (-d/--device)"
    exit 1
fi

if [[ -z "${MCL_PATH}" ]]; then
    log_error "mcl config path required (positional argument)"
    exit 1
fi

if [[ ! -e "${MCL_PATH}" ]]; then
    log_error "Config not found: ${MCL_PATH}"
    exit 1
fi

# Find CA cert
if [[ ! -f "${CERTS_DIR}/ca.crt" ]]; then
    log_error "CA certificate not found: ${CERTS_DIR}/ca.crt"
    exit 1
fi

# Find client cert/key - try device cert first, then bootstrap
if [[ -f "${CERTS_DIR}/device.crt" ]] && [[ -f "${CERTS_DIR}/device.key" ]]; then
    CLIENT_CERT="${CERTS_DIR}/device.crt"
    CLIENT_KEY="${CERTS_DIR}/device.key"
elif [[ -f "${CERTS_DIR}/bootstrap.crt" ]] && [[ -f "${CERTS_DIR}/bootstrap.key" ]]; then
    CLIENT_CERT="${CERTS_DIR}/bootstrap.crt"
    CLIENT_KEY="${CERTS_DIR}/bootstrap.key"
    log_warn "Using bootstrap cert - ACL may deny publish to device topic"
else
    log_error "No client certificate found in ${CERTS_DIR}"
    log_error "Expected device.crt/device.key or bootstrap.crt/bootstrap.key"
    exit 1
fi

# Check dependencies
for cmd in mosquitto_pub tar base64 jq; do
    if ! command -v "$cmd" &>/dev/null; then
        log_error "Required: $cmd"
        exit 1
    fi
done

# Default version
if [[ -z "${VERSION}" ]]; then
    VERSION="$(date +%Y%m%d-%H%M%S)"
fi

# MQTT connection args
MQTT_ARGS=(
    -h "${HOST}" -p "${PORT}"
    --cafile "${CERTS_DIR}/ca.crt"
    --cert "${CLIENT_CERT}"
    --key "${CLIENT_KEY}"
)

COMMAND_TOPIC="reefy/devices/${DEVICE_UUID}/commands"
STATUS_TOPIC="reefy/devices/${DEVICE_UUID}/status"

# Create tar.gz bundle
TMPDIR=$(mktemp -d)
trap "rm -rf ${TMPDIR}" EXIT

log_info "Packaging mcl config: ${MCL_PATH}"

if [[ -f "${MCL_PATH}" ]]; then
    # Single file - rename to main.mcl in the bundle
    cp "${MCL_PATH}" "${TMPDIR}/main.mcl"
    tar czf "${TMPDIR}/bundle.tar.gz" -C "${TMPDIR}" "main.mcl"
elif [[ -d "${MCL_PATH}" ]]; then
    # Directory - tar the contents (must contain main.mcl)
    if [[ ! -f "${MCL_PATH}/main.mcl" ]]; then
        # Check if there's exactly one .mcl file
        mcl_count=$(find "${MCL_PATH}" -maxdepth 1 -name '*.mcl' | wc -l | tr -d ' ')
        if [[ "${mcl_count}" -eq 0 ]]; then
            log_error "No .mcl files found in ${MCL_PATH}"
            exit 1
        fi
        log_warn "No main.mcl found - device will attempt auto-rename"
    fi
    tar czf "${TMPDIR}/bundle.tar.gz" -C "${MCL_PATH}" .
fi

BUNDLE_SIZE=$(wc -c < "${TMPDIR}/bundle.tar.gz" | tr -d ' ')
BUNDLE_B64=$(base64 < "${TMPDIR}/bundle.tar.gz")
B64_SIZE=${#BUNDLE_B64}

log_info "Bundle: ${BUNDLE_SIZE} bytes (${B64_SIZE} bytes base64)"

if [[ ${B64_SIZE} -gt 900000 ]]; then
    log_error "Bundle too large for MQTT (${B64_SIZE} bytes, max ~900KB)"
    exit 1
fi

# Build JSON payload
PAYLOAD=$(jq -n \
    --arg action "deploy_mcl" \
    --arg config_b64 "${BUNDLE_B64}" \
    --arg version "${VERSION}" \
    '{
        action: $action,
        config_b64: $config_b64,
        version: $version
    }')

log_info "Publishing to ${COMMAND_TOPIC}"
log_info "Version: ${VERSION}"

if mosquitto_pub "${MQTT_ARGS[@]}" \
    -t "${COMMAND_TOPIC}" \
    -m "${PAYLOAD}" \
    -q 1 2>/dev/null; then
    log_info "mcl config sent successfully"
else
    log_error "Failed to publish mcl config"
    exit 1
fi

# Watch for status response
if [[ "${WATCH}" == "true" ]]; then
    echo ""
    log_info "Watching for device status (30s timeout)..."
    echo ""

    timeout 30 mosquitto_sub "${MQTT_ARGS[@]}" \
        -t "${STATUS_TOPIC}" \
        -C 2 2>/dev/null | while IFS= read -r line; do
        state=$(echo "$line" | jq -r '.state // "unknown"' 2>/dev/null)
        message=$(echo "$line" | jq -r '.message // ""' 2>/dev/null)

        case "$state" in
            deploying)
                echo -e "${CYAN}[STATUS]${NC} ${state}: ${message}"
                ;;
            deployed)
                echo -e "${GREEN}[STATUS]${NC} ${state}: ${message}"
                ;;
            error)
                echo -e "${RED}[STATUS]${NC} ${state}: ${message}"
                ;;
            *)
                echo -e "[STATUS] ${state}: ${message}"
                ;;
        esac
    done || true

    echo ""
    log_info "Done"
fi
