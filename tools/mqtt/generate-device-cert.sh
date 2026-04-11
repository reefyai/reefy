#!/bin/bash
#
# Sign Device CSR - Create device certificate from a Certificate Signing Request
#
# The device generates its own RSA keypair and CSR. This script extracts the
# public key from the CSR and creates a certificate with a server-assigned UUID
# as the CN. The private key never leaves the device.
#
# Usage:
#   ./generate-device-cert.sh -r CSR_FILE -c CA_DIR [-u UUID] [-o OUTPUT_DIR] [-j]
#
# Options:
#   -r, --csr FILE          CSR file in PEM format (required)
#   -c, --ca-dir DIR        Directory containing CA cert and key (required)
#   -u, --uuid UUID         Device UUID (default: auto-generated)
#   -o, --output DIR        Output directory (default: ./device-certs/UUID)
#   -j, --json              Output certificate as JSON (for MQTT provisioning)
#   -h, --help              Show this help message
#
# Examples:
#   # Sign a CSR with auto-generated UUID
#   ./generate-device-cert.sh -r device.csr -c ./mqtt-server/certs
#
#   # Sign a CSR with specific UUID, output as JSON for MQTT
#   ./generate-device-cert.sh -r device.csr -c ./mqtt-server/certs -u my-device-uuid -j
#
#   # Read CSR from stdin (piped from MQTT registration)
#   echo "$CSR_PEM" | ./generate-device-cert.sh -r /dev/stdin -c ./mqtt-server/certs -j
#

set -euo pipefail

# Color output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# Defaults
CSR_FILE=""
CA_DIR=""
UUID=""
OUTPUT_DIR=""
JSON_OUTPUT=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -r|--csr)
            CSR_FILE="$2"
            shift 2
            ;;
        -c|--ca-dir)
            CA_DIR="$2"
            shift 2
            ;;
        -u|--uuid)
            UUID="$2"
            shift 2
            ;;
        -o|--output)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        -j|--json)
            JSON_OUTPUT=true
            shift
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

# Validate required arguments
if [[ -z "${CSR_FILE}" ]]; then
    log_error "CSR file is required (-r/--csr)"
    exit 1
fi

if [[ ! -f "${CSR_FILE}" ]] && [[ "${CSR_FILE}" != "/dev/stdin" ]]; then
    log_error "CSR file not found: ${CSR_FILE}"
    exit 1
fi

if [[ -z "${CA_DIR}" ]]; then
    log_error "CA directory is required (-c/--ca-dir)"
    exit 1
fi

if [[ ! -f "${CA_DIR}/ca.crt" ]] || [[ ! -f "${CA_DIR}/ca.key" ]]; then
    log_error "CA certificate or key not found in ${CA_DIR}"
    log_error "Expected: ${CA_DIR}/ca.crt and ${CA_DIR}/ca.key"
    exit 1
fi

# Auto-generate UUID if not provided
if [[ -z "${UUID}" ]]; then
    UUID=$(uuidgen 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null)
    if [[ -z "${UUID}" ]]; then
        log_error "Could not generate UUID"
        exit 1
    fi
fi

# Set default output directory
if [[ -z "${OUTPUT_DIR}" ]]; then
    OUTPUT_DIR="./device-certs/${UUID}"
fi

mkdir -p "${OUTPUT_DIR}"

log_info "Signing CSR for device UUID: ${UUID}"

# Verify the CSR is valid
if ! openssl req -in "${CSR_FILE}" -noout -verify 2>/dev/null; then
    log_error "Invalid CSR file: ${CSR_FILE}"
    exit 1
fi

# Extract public key from CSR
openssl req -in "${CSR_FILE}" -pubkey -noout > "${OUTPUT_DIR}/device.pub" 2>/dev/null

# Create extension file for client certificate
cat > "${OUTPUT_DIR}/device-ext.cnf" <<EOF
basicConstraints=CA:FALSE
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EOF

# Create certificate with server-assigned UUID as CN, using device's public key
# -force_pubkey: uses the device's public key (from CSR) instead of generating a new one
# -subj: server controls the certificate identity (CN=UUID for EMQX ACL matching)
openssl x509 -new \
    -force_pubkey "${OUTPUT_DIR}/device.pub" \
    -subj "/C=US/ST=FL/L=Miami/O=Reefy/OU=Devices/CN=${UUID}" \
    -CA "${CA_DIR}/ca.crt" -CAkey "${CA_DIR}/ca.key" \
    -CAcreateserial -days 3650 \
    -out "${OUTPUT_DIR}/device.crt" \
    -extfile "${OUTPUT_DIR}/device-ext.cnf" 2>/dev/null

# Cleanup temporary files
rm -f "${OUTPUT_DIR}/device.pub" "${OUTPUT_DIR}/device-ext.cnf"

log_info "Device certificate created: ${OUTPUT_DIR}/device.crt"

# Output as JSON if requested (for MQTT provisioning message)
if [[ "${JSON_OUTPUT}" == "true" ]]; then
    # Escape newlines for JSON
    cert_content=$(sed ':a;N;$!ba;s/\n/\\n/g' "${OUTPUT_DIR}/device.crt")

    cat <<EOF
{
  "uuid": "${UUID}",
  "device_cert": "${cert_content}",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
else
    log_info ""
    log_info "Certificate details:"
    openssl x509 -in "${OUTPUT_DIR}/device.crt" -noout -subject -dates -fingerprint -sha256
    log_info ""
    log_info "Files:"
    log_info "  Certificate: ${OUTPUT_DIR}/device.crt"
    log_info ""
    log_info "To provision via MQTT, run with -j flag for JSON output"
fi
