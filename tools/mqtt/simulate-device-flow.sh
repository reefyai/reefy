#!/bin/bash
#
# Simulate SBNB Device Registration Flow (CSR-based)
#
# This script simulates the complete device registration and provisioning flow:
# 1. Device generates RSA keypair and CSR
# 2. Device registers with bootstrap certificate, sending CSR
# 3. Admin approves device (signs CSR, publishes certificate)
# 4. Device receives certificate and reconnects with device certificate
#
# Usage:
#   ./simulate-device-flow.sh -c CERTS_DIR [-H HOST] [-p PORT]
#
# Options:
#   -c, --certs DIR         Directory containing certificates (required)
#   -H, --host HOST         MQTT broker host (default: localhost)
#   -p, --port PORT         MQTT broker port (default: 8883)
#   -h, --help              Show this help message
#
# This script requires two terminals:
#   Terminal 1: Run this script (simulates device)
#   Terminal 2: Run approve-devices.sh (admin approval)
#
# Example:
#   ./simulate-device-flow.sh -c ./mqtt-server/certs
#

set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log_device() { echo -e "${BLUE}[DEVICE]${NC} $*"; }
log_success() { echo -e "${GREEN}[OK]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# Defaults
CERTS_DIR=""
HOST="localhost"
PORT="8883"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -c|--certs)
            CERTS_DIR="$2"
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
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Validate
if [[ -z "${CERTS_DIR}" ]]; then
    log_error "Certificates directory is required (-c/--certs)"
    exit 1
fi

# Check dependencies
for cmd in mosquitto_pub mosquitto_sub jq openssl; do
    if ! command -v "$cmd" &>/dev/null; then
        log_error "Required: $cmd"
        exit 1
    fi
done

# MQTT connection args
MQTT_ARGS=(
    -h "${HOST}" -p "${PORT}"
    --cafile "${CERTS_DIR}/ca.crt"
    --cert "${CERTS_DIR}/bootstrap.crt"
    --key "${CERTS_DIR}/bootstrap.key"
)

# Generate simulated device identity
MAC_ADDRESS="02:$(openssl rand -hex 5 | sed 's/\(..\)/\1:/g; s/:$//')"
HOSTNAME="sbnb-$(echo ${MAC_ADDRESS} | tr -d ':' | tail -c 9)"
TEMP_DIR=$(mktemp -d)
trap "rm -rf ${TEMP_DIR}" EXIT

echo "=========================================="
echo "SBNB Device Flow Simulator (CSR-based)"
echo "=========================================="
echo "Broker:   ${HOST}:${PORT}"
echo "Device:   ${HOSTNAME}"
echo "MAC:      ${MAC_ADDRESS}"
echo ""

# Phase 1: Generate keypair + CSR
echo "=========================================="
echo "Phase 1: Generate Keypair + CSR"
echo "=========================================="

log_device "Generating RSA-2048 keypair..."
openssl genrsa -out "${TEMP_DIR}/device.key" 2048 2>/dev/null

log_device "Generating CSR..."
openssl req -new \
    -key "${TEMP_DIR}/device.key" \
    -out "${TEMP_DIR}/device.csr" \
    -subj "/O=SBNB/OU=Devices/CN=${HOSTNAME}" 2>/dev/null

CSR_PEM=$(cat "${TEMP_DIR}/device.csr")
log_success "Keypair and CSR generated (private key stays local)"
echo ""

# Phase 2: Register with CSR
echo "=========================================="
echo "Phase 2: Bootstrap Registration"
echo "=========================================="

log_device "Publishing registration with CSR..."

REGISTER_MSG=$(jq -n \
    --arg hostname "${HOSTNAME}" \
    --arg mac "${MAC_ADDRESS}" \
    --arg csr "${CSR_PEM}" \
    '{
        hostname: $hostname,
        mac: $mac,
        timestamp: now,
        csr: $csr
    }')

REGISTER_TOPIC="sbnb/devices/bootstrap/${HOSTNAME}/register"
PROVISION_TOPIC="sbnb/devices/bootstrap/${HOSTNAME}/provision"

if mosquitto_pub "${MQTT_ARGS[@]}" \
    -t "${REGISTER_TOPIC}" \
    -m "${REGISTER_MSG}" \
    -q 1 -r 2>/dev/null; then
    log_success "Registration sent to ${REGISTER_TOPIC}"
else
    log_error "Failed to publish registration"
    exit 1
fi

echo ""
echo "=========================================="
echo "Waiting for Admin Approval"
echo "=========================================="
echo ""
echo "Device is waiting for provisioning on: ${PROVISION_TOPIC}"
echo ""
echo "To approve, run in another terminal:"
echo ""
echo -e "  ${CYAN}./approve-devices.sh -c ${CERTS_DIR} -H ${HOST} -p ${PORT}${NC}"
echo ""
echo -e "${YELLOW}Listening for provisioning response (5 min timeout)...${NC}"
echo ""

# Subscribe to provisioning response
PROVISION_FILE="${TEMP_DIR}/provision.json"
timeout 300 mosquitto_sub "${MQTT_ARGS[@]}" \
    -t "${PROVISION_TOPIC}" \
    -C 1 > "${PROVISION_FILE}" 2>/dev/null || {
    log_error "Timeout waiting for provisioning (5 minutes)"
    exit 1
}

log_success "Received provisioning response!"

# Parse response - extract certificate only (no private key in CSR flow)
UUID=$(jq -r '.uuid' "${PROVISION_FILE}")
DEVICE_CERT_DIR="${TEMP_DIR}/device-cert"
mkdir -p "${DEVICE_CERT_DIR}"

jq -r '.device_cert' "${PROVISION_FILE}" | sed 's/\\n/\n/g' > "${DEVICE_CERT_DIR}/device.crt"
cp "${TEMP_DIR}/device.key" "${DEVICE_CERT_DIR}/device.key"

echo "UUID: ${UUID}"

# Verify certificate
if openssl x509 -in "${DEVICE_CERT_DIR}/device.crt" -noout -subject 2>/dev/null; then
    log_success "Certificate valid:"
    openssl x509 -in "${DEVICE_CERT_DIR}/device.crt" -noout -subject -dates
else
    log_error "Invalid certificate received"
    exit 1
fi

# Verify cert matches our private key
CERT_MODULUS=$(openssl x509 -in "${DEVICE_CERT_DIR}/device.crt" -noout -modulus 2>/dev/null | md5sum)
KEY_MODULUS=$(openssl rsa -in "${DEVICE_CERT_DIR}/device.key" -noout -modulus 2>/dev/null | md5sum)
if [[ "${CERT_MODULUS}" == "${KEY_MODULUS}" ]]; then
    log_success "Certificate matches device private key"
else
    log_error "Certificate does NOT match device private key!"
    exit 1
fi

echo ""

# Phase 3: Reconnect with device certificate
echo "=========================================="
echo "Phase 3: Reconnect with Device Certificate"
echo "=========================================="

log_device "Publishing status with device certificate..."

STATUS_MSG=$(jq -n \
    --arg hostname "${HOSTNAME}" \
    --arg mac "${MAC_ADDRESS}" \
    '{
        status: "online",
        hostname: $hostname,
        mac: $mac,
        timestamp: now
    }')

DEVICE_MQTT_ARGS=(
    -h "${HOST}" -p "${PORT}"
    --cafile "${CERTS_DIR}/ca.crt"
    --cert "${DEVICE_CERT_DIR}/device.crt"
    --key "${DEVICE_CERT_DIR}/device.key"
)

if mosquitto_pub "${DEVICE_MQTT_ARGS[@]}" \
    -t "sbnb/devices/${UUID}/status" \
    -m "${STATUS_MSG}" \
    -q 1 2>/dev/null; then
    log_success "Status sent with device certificate (CN=${UUID})"
else
    log_error "Failed to send status with device certificate"
    exit 1
fi

echo ""
echo "=========================================="
echo "Phase 4: Listen for Commands"
echo "=========================================="
echo ""
log_device "Subscribing to: sbnb/devices/${UUID}/commands"
echo ""
echo -e "${YELLOW}Listening for commands (Ctrl+C to exit)...${NC}"
echo ""

mosquitto_sub "${DEVICE_MQTT_ARGS[@]}" \
    -t "sbnb/devices/${UUID}/commands" \
    -v | while read -r line; do
    echo ""
    log_success "Received command:"
    echo "${line}"
    echo ""
done
