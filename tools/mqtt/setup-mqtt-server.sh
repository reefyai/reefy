#!/bin/bash
#
# SBNB MQTT Server Setup Script
#
# This script sets up a complete MQTT+mTLS infrastructure for SBNB devices:
# 1. Generates CA and bootstrap certificates
# 2. Configures EMQX broker with mTLS
# 3. Starts broker in Docker
# 4. Prepares USB flash bundle
#
# Usage:
#   ./setup-mqtt-server.sh [OPTIONS]
#
# Options:
#   -d, --domain DOMAIN     MQTT broker domain or IP (default: mqtt.example.com)
#   -p, --port PORT         MQTT broker port (default: 8883)
#   -o, --output DIR        Output directory (default: ./mqtt-server)
#   -s, --start-broker      Start EMQX broker in Docker
#   --skip-certs            Skip certificate generation (use existing)
#   -h, --help              Show this help message
#
# Examples:
#   # Generate certs and prepare USB bundle
#   ./setup-mqtt-server.sh -d mqtt.mycompany.com -o ./mqtt-server
#
#   # Use IP address instead of domain
#   ./setup-mqtt-server.sh -d 192.168.40.42 -o ./mqtt-server
#
#   # Start broker with existing certs
#   ./setup-mqtt-server.sh --skip-certs -s -o ./mqtt-server
#
# To inject MQTT config into a raw image, use inject-mqtt-config.sh:
#   sudo ./inject-mqtt-config.sh -i /path/to/sbnb.raw
#

set -euo pipefail

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# Defaults
MQTT_DOMAIN="mqtt.example.com"
MQTT_PORT="8883"
OUTPUT_DIR="./mqtt-server"
START_BROKER=false
SKIP_CERTS=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--domain)
            MQTT_DOMAIN="$2"
            shift 2
            ;;
        -p|--port)
            MQTT_PORT="$2"
            shift 2
            ;;
        -o|--output)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        -s|--start-broker)
            START_BROKER=true
            shift
            ;;
        --skip-certs)
            SKIP_CERTS=true
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

# Create output directory structure
log_info "Creating output directory structure at ${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"/{certs,broker-config,usb-bundle/mqtt,device-certs}

CERTS_DIR="${OUTPUT_DIR}/certs"
BROKER_CONFIG_DIR="${OUTPUT_DIR}/broker-config"
USB_BUNDLE_DIR="${OUTPUT_DIR}/usb-bundle/mqtt"
DEVICE_CERTS_DIR="${OUTPUT_DIR}/device-certs"

# ============================================================================
# Certificate Generation
# ============================================================================

generate_certificates() {
    log_info "Generating certificate hierarchy..."

    cd "${CERTS_DIR}"

    # Generate CA certificate (valid for 10 years)
    log_info "Generating CA certificate..."
    openssl req -new -x509 -days 3650 -extensions v3_ca \
        -keyout ca.key -out ca.crt \
        -subj "/C=US/ST=FL/L=Miami/O=SBNB/OU=Certificate Authority/CN=SBNB Root CA" \
        -nodes 2>/dev/null

    log_info "✓ CA certificate: ${CERTS_DIR}/ca.crt"

    # Generate broker/server certificate
    log_info "Generating broker server certificate for ${MQTT_DOMAIN}..."
    openssl genrsa -out broker.key 2048 2>/dev/null
    openssl req -new -key broker.key -out broker.csr \
        -subj "/C=US/ST=FL/L=Miami/O=SBNB/OU=Broker/CN=${MQTT_DOMAIN}" 2>/dev/null

    # Create broker cert with SAN extension
    # Auto-detect if domain is an IP address or hostname
    local san_entries="DNS.1 = localhost"$'\n'"IP.1 = 127.0.0.1"
    if [[ "${MQTT_DOMAIN}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        san_entries="${san_entries}"$'\n'"IP.2 = ${MQTT_DOMAIN}"
    else
        san_entries="DNS.1 = ${MQTT_DOMAIN}"$'\n'"DNS.2 = localhost"$'\n'"IP.1 = 127.0.0.1"
    fi

    cat > broker-ext.cnf <<EOF
basicConstraints=CA:FALSE
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
${san_entries}
EOF

    openssl x509 -req -in broker.csr -CA ca.crt -CAkey ca.key \
        -CAcreateserial -out broker.crt -days 3650 \
        -extfile broker-ext.cnf 2>/dev/null

    rm broker.csr broker-ext.cnf
    log_info "✓ Broker certificate: ${CERTS_DIR}/broker.crt"

    # Generate bootstrap certificate (shared across devices for initial registration)
    log_info "Generating shared bootstrap certificate..."
    openssl genrsa -out bootstrap.key 2048 2>/dev/null
    openssl req -new -key bootstrap.key -out bootstrap.csr \
        -subj "/C=US/ST=FL/L=Miami/O=SBNB/OU=Devices/CN=bootstrap" 2>/dev/null

    cat > bootstrap-ext.cnf <<EOF
basicConstraints=CA:FALSE
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EOF

    openssl x509 -req -in bootstrap.csr -CA ca.crt -CAkey ca.key \
        -CAcreateserial -out bootstrap.crt -days 3650 \
        -extfile bootstrap-ext.cnf 2>/dev/null

    rm bootstrap.csr bootstrap-ext.cnf
    log_info "✓ Bootstrap certificate: ${CERTS_DIR}/bootstrap.crt"

    # Make certs readable by EMQX container (runs as uid 1000)
    chmod 644 ca.crt broker.crt bootstrap.crt ca.key broker.key bootstrap.key

    cd - > /dev/null

    log_info "Certificate generation complete!"
    log_info ""
    log_info "Certificate fingerprints:"
    log_info "  CA:        $(openssl x509 -noout -fingerprint -sha256 -in ${CERTS_DIR}/ca.crt | cut -d= -f2)"
    log_info "  Broker:    $(openssl x509 -noout -fingerprint -sha256 -in ${CERTS_DIR}/broker.crt | cut -d= -f2)"
    log_info "  Bootstrap: $(openssl x509 -noout -fingerprint -sha256 -in ${CERTS_DIR}/bootstrap.crt | cut -d= -f2)"
}

# Function to generate device-specific certificate
generate_device_cert() {
    local uuid="$1"
    local output_dir="${DEVICE_CERTS_DIR}/${uuid}"

    mkdir -p "${output_dir}"
    cd "${output_dir}"

    log_info "Generating device certificate for UUID: ${uuid}"

    openssl genrsa -out device.key 2048 2>/dev/null
    openssl req -new -key device.key -out device.csr \
        -subj "/C=US/ST=FL/L=Miami/O=SBNB/OU=Devices/CN=${uuid}" 2>/dev/null

    cat > device-ext.cnf <<EOF
basicConstraints=CA:FALSE
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EOF

    openssl x509 -req -in device.csr -CA "${CERTS_DIR}/ca.crt" -CAkey "${CERTS_DIR}/ca.key" \
        -CAcreateserial -out device.crt -days 3650 \
        -extfile device-ext.cnf 2>/dev/null

    rm device.csr device-ext.cnf

    cd - > /dev/null
    log_info "✓ Device certificate: ${output_dir}/device.crt"
}

# ============================================================================
# EMQX Broker Configuration
# ============================================================================

create_broker_config() {
    log_info "Creating EMQX broker configuration..."

    # Create EMQX ACL configuration (file-based authorization)
    cat > "${BROKER_CONFIG_DIR}/acl.conf" <<'EOF'
%% SBNB MQTT Access Control List for EMQX
%% Syntax: {allow|deny, who, action, topics}.
%% action: publish | subscribe | all
%% who: all | {user, "name"} | {clientid, "id"}

%% Bootstrap certificate (CN=bootstrap) can publish/subscribe to bootstrap topic tree
%% Devices publish registration to: sbnb/devices/bootstrap/{hostname}/register
%% Admin publishes provisioning to: sbnb/devices/bootstrap/{hostname}/provision
{allow, {user, "bootstrap"}, publish, ["sbnb/devices/bootstrap/#"]}.
{allow, {user, "bootstrap"}, subscribe, ["sbnb/devices/bootstrap/#"]}.

%% Admin (bootstrap cert) can send commands to devices and monitor status
{allow, {user, "bootstrap"}, publish, ["sbnb/devices/+/commands"]}.
{allow, {user, "bootstrap"}, subscribe, ["sbnb/devices/+/status"]}.

%% Any authenticated user can publish and subscribe to their own topics
%% ${username} is replaced with the CN extracted from the client certificate
{allow, all, all, ["sbnb/devices/${username}/#"]}.

%% Deny all other access
{deny, all}.
EOF

    # Create EMQX environment configuration
    cat > "${BROKER_CONFIG_DIR}/emqx.env" <<EOF
# EMQX Configuration via Environment Variables

# Cluster and node
EMQX_NAME=sbnb-mqtt-broker
EMQX_HOST=127.0.0.1

# Disable unused listeners (only allow TLS on 8883)
EMQX_LISTENERS__TCP__DEFAULT__ENABLE=false
EMQX_LISTENERS__WS__DEFAULT__ENABLE=false
EMQX_LISTENERS__WSS__DEFAULT__ENABLE=false

# SSL/TLS Listener configuration
EMQX_LISTENERS__SSL__DEFAULT__BIND=8883
EMQX_LISTENERS__SSL__DEFAULT__SSL_OPTIONS__CACERTFILE=/opt/emqx/etc/certs/ca.crt
EMQX_LISTENERS__SSL__DEFAULT__SSL_OPTIONS__CERTFILE=/opt/emqx/etc/certs/broker.crt
EMQX_LISTENERS__SSL__DEFAULT__SSL_OPTIONS__KEYFILE=/opt/emqx/etc/certs/broker.key
EMQX_LISTENERS__SSL__DEFAULT__SSL_OPTIONS__VERIFY=verify_peer
EMQX_LISTENERS__SSL__DEFAULT__SSL_OPTIONS__FAIL_IF_NO_PEER_CERT=true

# Use CN from client certificate as MQTT username
# (extracted value is just "bootstrap" or UUID, not "CN=bootstrap")
EMQX_MQTT__PEER_CERT_AS_USERNAME=cn

# Authentication: mTLS handles auth (verify_peer + fail_if_no_peer_cert)
# No additional auth backend needed - TLS handshake rejects invalid certs

# Authorization: use file-based ACL
EMQX_AUTHORIZATION__SOURCES__1__TYPE=file
EMQX_AUTHORIZATION__SOURCES__1__PATH=/opt/emqx/etc/acl.conf
EMQX_AUTHORIZATION__SOURCES__1__ENABLE=true
EMQX_AUTHORIZATION__NO_MATCH=deny
EMQX_AUTHORIZATION__DENY_ACTION=disconnect

# Dashboard (web UI) - accessible on http://localhost:18083
EMQX_DASHBOARD__LISTENERS__HTTP__BIND=18083
EMQX_DASHBOARD__DEFAULT_USERNAME=admin
EMQX_DASHBOARD__DEFAULT_PASSWORD=public

# Logging
EMQX_LOG__CONSOLE__ENABLE=true
EMQX_LOG__CONSOLE__LEVEL=info
EOF

    # Create docker-compose.yml
    cat > "${BROKER_CONFIG_DIR}/docker-compose.yml" <<EOF
version: '3.8'

services:
  emqx:
    image: emqx/emqx:5.8.9
    container_name: sbnb-mqtt-broker
    restart: unless-stopped
    ports:
      - "${MQTT_PORT}:8883"      # MQTT over TLS
      - "18083:18083"            # Dashboard (HTTP)
    volumes:
      - ./acl.conf:/opt/emqx/etc/acl.conf:ro
      - ../certs:/opt/emqx/etc/certs:ro
      - emqx-data:/opt/emqx/data
      - emqx-log:/opt/emqx/log
    env_file:
      - emqx.env

    networks:
      - sbnb-mqtt
    healthcheck:
      test: ["CMD", "/opt/emqx/bin/emqx", "ctl", "status"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s

volumes:
  emqx-data:
    driver: local
  emqx-log:
    driver: local

networks:
  sbnb-mqtt:
    driver: bridge
EOF

    log_info "✓ EMQX ACL configuration: ${BROKER_CONFIG_DIR}/acl.conf"
    log_info "✓ EMQX environment config: ${BROKER_CONFIG_DIR}/emqx.env"
    log_info "✓ Docker Compose: ${BROKER_CONFIG_DIR}/docker-compose.yml"
}

# ============================================================================
# USB Bundle Preparation
# ============================================================================

create_usb_bundle() {
    log_info "Creating USB flash bundle..."

    # Copy certificates to USB bundle
    cp "${CERTS_DIR}/ca.crt" "${USB_BUNDLE_DIR}/"
    cp "${CERTS_DIR}/bootstrap.crt" "${USB_BUNDLE_DIR}/"
    cp "${CERTS_DIR}/bootstrap.key" "${USB_BUNDLE_DIR}/"

    # Create mqtt.conf
    cat > "${USB_BUNDLE_DIR}/mqtt.conf" <<EOF
# SBNB MQTT Configuration
# This file is copied from USB flash to /etc/sbnb/mqtt/ at boot

MQTT_BROKER=${MQTT_DOMAIN}
MQTT_PORT=${MQTT_PORT}
MQTT_KEEPALIVE=60

# Certificate paths (after copying to /etc/sbnb/mqtt/)
MQTT_CA_CERT=/etc/sbnb/mqtt/ca.crt
MQTT_CLIENT_CERT=/etc/sbnb/mqtt/bootstrap.crt
MQTT_CLIENT_KEY=/etc/sbnb/mqtt/bootstrap.key

# Topic prefix
MQTT_TOPIC_PREFIX=sbnb
EOF

    # Create README for USB bundle
    cat > "${USB_BUNDLE_DIR}/README.txt" <<EOF
SBNB MQTT Configuration Bundle
================================

This directory contains the MQTT configuration and certificates for SBNB devices.

Files:
  - mqtt.conf       : MQTT broker connection settings
  - ca.crt          : CA certificate (verifies broker identity)
  - bootstrap.crt   : Shared bootstrap certificate (initial device registration)
  - bootstrap.key   : Bootstrap private key

Usage:
  1. Copy this entire 'mqtt/' directory to the root of your SBNB USB flash drive
  2. The directory structure should be:
       /mnt/sbnb/mqtt/mqtt.conf
       /mnt/sbnb/mqtt/ca.crt
       /mnt/sbnb/mqtt/bootstrap.crt
       /mnt/sbnb/mqtt/bootstrap.key
  3. Insert USB into SBNB device and boot
  4. The boot script will automatically copy these files to /etc/sbnb/mqtt/
  5. The sbnb-mqtt service will start and connect to the broker

Security Notes:
  - The bootstrap certificate is shared across devices (for initial registration)
  - After provisioning, each device gets a unique certificate
  - Keep the CA certificate and bootstrap key secure but not secret
  - Rotate bootstrap certificate periodically if compromised

For more information, see: docs/README-MQTT.md
EOF

    log_info "✓ USB bundle created: ${USB_BUNDLE_DIR}/"
    log_info ""
    log_info "USB bundle contents:"
    ls -lh "${USB_BUNDLE_DIR}/" | tail -n +2
}

# ============================================================================
# Start Broker
# ============================================================================

start_broker() {
    log_info "Starting EMQX MQTT broker with Docker Compose..."

    cd "${BROKER_CONFIG_DIR}"

    # Check if docker-compose or docker compose is available
    if command -v docker-compose &> /dev/null; then
        DOCKER_COMPOSE="docker-compose"
    elif docker compose version &> /dev/null; then
        DOCKER_COMPOSE="docker compose"
    else
        log_error "Docker Compose not found. Install docker-compose or use Docker with compose plugin"
        exit 1
    fi

    # Stop existing container if running
    ${DOCKER_COMPOSE} down 2>/dev/null || true

    # Start broker
    ${DOCKER_COMPOSE} up -d

    # Wait for broker to be healthy
    log_info "Waiting for EMQX to be ready..."
    sleep 5

    cd - > /dev/null

    log_info "✓ EMQX broker started successfully"
    log_info ""
    log_info "Broker information:"
    log_info "  MQTT URL:     mqtts://${MQTT_DOMAIN}:${MQTT_PORT}"
    log_info "  Dashboard:    http://localhost:18083 (admin/public)"
    log_info "  View logs:    cd ${BROKER_CONFIG_DIR} && ${DOCKER_COMPOSE} logs -f"
    log_info "  Stop broker:  cd ${BROKER_CONFIG_DIR} && ${DOCKER_COMPOSE} down"
    log_info ""
    log_info "Dashboard features:"
    log_info "  - Real-time client connections and topics"
    log_info "  - Message flow visualization"
    log_info "  - ACL rule testing"
    log_info "  - Metrics and monitoring"
}

# ============================================================================
# Main Execution
# ============================================================================

main() {
    log_info "SBNB MQTT Server Setup"
    log_info "======================"
    log_info "Domain:     ${MQTT_DOMAIN}"
    log_info "Port:       ${MQTT_PORT}"
    log_info "Output:     ${OUTPUT_DIR}"
    log_info ""

    # Generate certificates, broker config, and USB bundle if needed
    if [[ "${SKIP_CERTS}" == "false" ]]; then
        generate_certificates
        create_broker_config
        create_usb_bundle
    else
        log_warn "Skipping certificate generation (using existing)"
        if [[ ! -f "${CERTS_DIR}/ca.crt" ]]; then
            log_error "No existing certificates found in ${CERTS_DIR}"
            log_error "Did you forget -o? Example: $0 --skip-certs -o ./mqtt-server"
            exit 1
        fi
        if [[ ! -f "${USB_BUNDLE_DIR}/mqtt.conf" ]]; then
            log_error "No existing USB bundle found in ${USB_BUNDLE_DIR}"
            exit 1
        fi
        log_info "Using existing USB bundle: ${USB_BUNDLE_DIR}/"
    fi

    # Start broker if requested
    if [[ "${START_BROKER}" == "true" ]]; then
        start_broker
    fi

    # Summary
    log_info ""
    log_info "======================================"
    log_info "Setup complete!"
    log_info "======================================"
    log_info ""
    log_info "Next steps:"
    log_info ""
    if [[ "${START_BROKER}" == "false" ]]; then
        log_info "1. Start the MQTT broker:"
        log_info "   $0 --skip-certs -s -o ${OUTPUT_DIR}"
        log_info ""
    fi
    log_info "2. Copy USB bundle to your SBNB device:"
    log_info "   cp -r ${USB_BUNDLE_DIR} /path/to/usb/mount/point/"
    log_info ""
    log_info "3. Or inject into raw image:"
    log_info "   sudo ./inject-mqtt-config.sh -o ${OUTPUT_DIR} -i /path/to/sbnb.raw"
    log_info ""
    log_info "4. Boot SBNB device with MQTT config"
    log_info "   The device will automatically register with the broker"
    log_info ""
    log_info "Files generated:"
    log_info "  Certificates:   ${CERTS_DIR}/"
    log_info "  Broker config:  ${BROKER_CONFIG_DIR}/"
    log_info "  USB bundle:     ${USB_BUNDLE_DIR}/"
    log_info "  Device certs:   ${DEVICE_CERTS_DIR}/ (generated on-demand)"
    log_info ""
}

main "$@"
