#!/bin/bash
#
# Test EMQX MQTT Broker Setup
#
# This script tests the EMQX broker configuration and certificate authentication.
#
# Usage:
#   ./test-mqtt-broker.sh -c CERTS_DIR [-h HOST] [-p PORT]
#
# Options:
#   -c, --certs DIR         Directory containing certificates (required)
#   -h, --host HOST         MQTT broker host (default: localhost)
#   -p, --port PORT         MQTT broker port (default: 8883)
#   --help                  Show this help message
#
# Examples:
#   # Test local broker
#   ./test-mqtt-broker.sh -c ./mqtt-server/certs
#
#   # Test remote broker
#   ./test-mqtt-broker.sh -c ./mqtt-server/certs -h mqtt.example.com
#

set -euo pipefail

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}✓${NC} $*"; }
log_error() { echo -e "${RED}✗${NC} $*"; }
log_warn() { echo -e "${YELLOW}!${NC} $*"; }
log_test() { echo -e "${BLUE}→${NC} $*"; }

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
        -h|--host)
            HOST="$2"
            shift 2
            ;;
        -p|--port)
            PORT="$2"
            shift 2
            ;;
        --help)
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

# Validate
if [[ -z "${CERTS_DIR}" ]]; then
    log_error "Certificates directory is required (-c/--certs)"
    exit 1
fi

if [[ ! -d "${CERTS_DIR}" ]]; then
    log_error "Certificates directory not found: ${CERTS_DIR}"
    exit 1
fi

# Check required certificates
required_certs=("ca.crt" "bootstrap.crt" "bootstrap.key" "broker.crt")
for cert in "${required_certs[@]}"; do
    if [[ ! -f "${CERTS_DIR}/${cert}" ]]; then
        log_error "Missing certificate: ${CERTS_DIR}/${cert}"
        exit 1
    fi
done

# Check dependencies
if ! command -v mosquitto_pub &> /dev/null; then
    log_error "mosquitto_pub not found. Install mosquitto-clients:"
    echo "  Ubuntu/Debian: sudo apt-get install mosquitto-clients"
    echo "  Fedora/RHEL:   sudo dnf install mosquitto"
    echo "  macOS:         brew install mosquitto"
    exit 1
fi

echo "=========================================="
echo "Reefy MQTT Broker Test"
echo "=========================================="
echo "Broker:  ${HOST}:${PORT}"
echo "Certs:   ${CERTS_DIR}"
echo ""

TESTS_PASSED=0
TESTS_FAILED=0

# Test 1: TLS Connection
log_test "Test 1: TLS connection with openssl"
if openssl s_client -connect "${HOST}:${PORT}" \
    -CAfile "${CERTS_DIR}/ca.crt" \
    -cert "${CERTS_DIR}/bootstrap.crt" \
    -key "${CERTS_DIR}/bootstrap.key" \
    -verify_return_error </dev/null 2>&1 | grep -q "Verify return code: 0"; then
    log_info "TLS connection successful"
    ((TESTS_PASSED++))
else
    log_error "TLS connection failed"
    ((TESTS_FAILED++))
fi
echo ""

# Test 2: Certificate Verification
log_test "Test 2: Verify certificate chain"
if openssl verify -CAfile "${CERTS_DIR}/ca.crt" "${CERTS_DIR}/bootstrap.crt" &>/dev/null; then
    log_info "Bootstrap certificate chain valid"
    ((TESTS_PASSED++))
else
    log_error "Bootstrap certificate chain invalid"
    ((TESTS_FAILED++))
fi

if openssl verify -CAfile "${CERTS_DIR}/ca.crt" "${CERTS_DIR}/broker.crt" &>/dev/null; then
    log_info "Broker certificate chain valid"
    ((TESTS_PASSED++))
else
    log_error "Broker certificate chain invalid"
    ((TESTS_FAILED++))
fi
echo ""

# Test 3: Bootstrap Certificate - Publish to bootstrap topic
log_test "Test 3: Publish to bootstrap topic (should succeed)"
test_message='{"mac":"00:11:22:33:44:55","hostname":"test-device","timestamp":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}'
if timeout 5 mosquitto_pub -h "${HOST}" -p "${PORT}" \
    --cafile "${CERTS_DIR}/ca.crt" \
    --cert "${CERTS_DIR}/bootstrap.crt" \
    --key "${CERTS_DIR}/bootstrap.key" \
    -t 'reefy/devices/bootstrap' \
    -m "${test_message}" \
    -q 1 2>/dev/null; then
    log_info "Bootstrap publish successful"
    ((TESTS_PASSED++))
else
    log_error "Bootstrap publish failed"
    ((TESTS_FAILED++))
fi
echo ""

# Test 4: Bootstrap Certificate - Try to publish to restricted topic (should fail)
log_test "Test 4: Publish to restricted topic (should fail due to ACL)"
if timeout 5 mosquitto_pub -h "${HOST}" -p "${PORT}" \
    --cafile "${CERTS_DIR}/ca.crt" \
    --cert "${CERTS_DIR}/bootstrap.crt" \
    --key "${CERTS_DIR}/bootstrap.key" \
    -t 'reefy/devices/test-uuid/config' \
    -m '{"test":"should-fail"}' \
    -q 1 2>/dev/null; then
    log_error "ACL enforcement failed - bootstrap should not access device topics"
    ((TESTS_FAILED++))
else
    log_info "ACL enforcement working - bootstrap cannot access device topics"
    ((TESTS_PASSED++))
fi
echo ""

# Test 5: Subscribe to bootstrap topic
log_test "Test 5: Subscribe to bootstrap topic (timeout after 2s)"
if timeout 2 mosquitto_sub -h "${HOST}" -p "${PORT}" \
    --cafile "${CERTS_DIR}/ca.crt" \
    --cert "${CERTS_DIR}/bootstrap.crt" \
    --key "${CERTS_DIR}/bootstrap.key" \
    -t 'reefy/devices/bootstrap' \
    -C 1 2>/dev/null || [[ $? -eq 124 ]]; then
    log_info "Subscribe successful (timeout is expected)"
    ((TESTS_PASSED++))
else
    log_error "Subscribe failed"
    ((TESTS_FAILED++))
fi
echo ""

# Test 6: Pub/Sub round-trip
log_test "Test 6: Publish and subscribe round-trip"
TEMP_MSG="test-message-$$-$(date +%s)"
(
    sleep 1
    mosquitto_pub -h "${HOST}" -p "${PORT}" \
        --cafile "${CERTS_DIR}/ca.crt" \
        --cert "${CERTS_DIR}/bootstrap.crt" \
        --key "${CERTS_DIR}/bootstrap.key" \
        -t 'reefy/devices/bootstrap' \
        -m "${TEMP_MSG}" \
        -q 1 2>/dev/null
) &
PUB_PID=$!

if timeout 5 mosquitto_sub -h "${HOST}" -p "${PORT}" \
    --cafile "${CERTS_DIR}/ca.crt" \
    --cert "${CERTS_DIR}/bootstrap.crt" \
    --key "${CERTS_DIR}/bootstrap.key" \
    -t 'reefy/devices/bootstrap' \
    -C 1 2>/dev/null | grep -q "${TEMP_MSG}"; then
    log_info "Pub/Sub round-trip successful"
    ((TESTS_PASSED++))
else
    log_error "Pub/Sub round-trip failed"
    ((TESTS_FAILED++))
fi
wait $PUB_PID 2>/dev/null || true
echo ""

# Test 7: Test device certificate if available
if [[ -d "${CERTS_DIR}/../device-certs" ]]; then
    DEVICE_CERT_DIR=$(find "${CERTS_DIR}/../device-certs" -mindepth 1 -maxdepth 1 -type d | head -n1)
    if [[ -n "${DEVICE_CERT_DIR}" ]]; then
        UUID=$(basename "${DEVICE_CERT_DIR}")
        log_test "Test 7: Device certificate test (UUID: ${UUID})"

        if [[ -f "${DEVICE_CERT_DIR}/device.crt" ]] && [[ -f "${DEVICE_CERT_DIR}/device.key" ]]; then
            # Test device can publish to its own topic
            if timeout 5 mosquitto_pub -h "${HOST}" -p "${PORT}" \
                --cafile "${CERTS_DIR}/ca.crt" \
                --cert "${DEVICE_CERT_DIR}/device.crt" \
                --key "${DEVICE_CERT_DIR}/device.key" \
                -t "reefy/devices/${UUID}/status" \
                -m '{"status":"online"}' \
                -q 1 2>/dev/null; then
                log_info "Device can publish to own topic"
                ((TESTS_PASSED++))
            else
                log_error "Device cannot publish to own topic"
                ((TESTS_FAILED++))
            fi

            # Test device cannot publish to bootstrap topic
            if timeout 5 mosquitto_pub -h "${HOST}" -p "${PORT}" \
                --cafile "${CERTS_DIR}/ca.crt" \
                --cert "${DEVICE_CERT_DIR}/device.crt" \
                --key "${DEVICE_CERT_DIR}/device.key" \
                -t 'reefy/devices/bootstrap' \
                -m '{"test":"should-fail"}' \
                -q 1 2>/dev/null; then
                log_error "Device should not access bootstrap topic"
                ((TESTS_FAILED++))
            else
                log_info "Device correctly restricted from bootstrap topic"
                ((TESTS_PASSED++))
            fi
        else
            log_warn "Device certificate files not found, skipping device tests"
        fi
        echo ""
    fi
fi

# Summary
echo "=========================================="
echo "Test Summary"
echo "=========================================="
if [[ ${TESTS_FAILED} -eq 0 ]]; then
    log_info "All tests passed: ${TESTS_PASSED}/${TESTS_PASSED}"
    echo ""
    echo "Your MQTT broker is configured correctly!"
    echo ""
    echo "Next steps:"
    echo "  1. Copy USB bundle to Reefy device"
    echo "  2. Boot device with MQTT configuration"
    echo "  3. Monitor broker logs for device registration"
    exit 0
else
    log_error "Tests failed: ${TESTS_FAILED}, passed: ${TESTS_PASSED}"
    echo ""
    echo "Troubleshooting:"
    echo "  1. Check broker is running: docker compose ps"
    echo "  2. View broker logs: docker compose logs -f"
    echo "  3. Verify certificates: openssl verify -CAfile ca.crt bootstrap.crt"
    echo "  4. Test TLS connection: openssl s_client -connect ${HOST}:${PORT}"
    exit 1
fi
