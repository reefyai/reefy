#!/bin/bash
#
# Interactive Device Approval Tool
#
# Connects to MQTT broker, discovers pending device registrations,
# and allows the admin to approve them by signing their CSRs.
#
# Usage:
#   ./approve-devices.sh -c CERTS_DIR [-H HOST] [-p PORT] [-w WAIT]
#
# Options:
#   -c, --certs DIR         Certificate directory (required)
#   -H, --host HOST         MQTT broker host (default: localhost)
#   -p, --port PORT         MQTT broker port (default: 8883)
#   -w, --wait SECONDS      Wait time for collecting registrations (default: 3)
#   -h, --help              Show this help message
#
# The tool subscribes to reefy/devices/bootstrap/+/register to discover
# devices that have registered with a CSR and are waiting for approval.
# On approval, it signs the CSR, assigns a UUID, and publishes the
# signed certificate back to the device.
#
# Example:
#   ./approve-devices.sh -c ./mqtt-server/certs -H mqtt.example.com
#

set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Defaults
CERTS_DIR=""
HOST="localhost"
PORT="8883"
WAIT=3

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
        -w|--wait)
            WAIT="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# Validate
if [[ -z "${CERTS_DIR}" ]]; then
    echo "Certificates directory required (-c/--certs)" >&2
    exit 1
fi

for f in ca.crt bootstrap.crt bootstrap.key; do
    if [[ ! -f "${CERTS_DIR}/${f}" ]]; then
        echo "Missing ${CERTS_DIR}/${f}" >&2
        exit 1
    fi
done

# Check dependencies
for cmd in mosquitto_sub mosquitto_pub jq; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "Required: $cmd" >&2
        [[ "$cmd" == "jq" ]] && echo "  Install: sudo apt-get install jq" >&2
        [[ "$cmd" == mosquitto_* ]] && echo "  Install: sudo apt-get install mosquitto-clients" >&2
        exit 1
    fi
done

# MQTT connection args (reused everywhere)
MQTT_ARGS=(
    -h "${HOST}" -p "${PORT}"
    --cafile "${CERTS_DIR}/ca.crt"
    --cert "${CERTS_DIR}/bootstrap.crt"
    --key "${CERTS_DIR}/bootstrap.key"
)

# Collect pending registrations from MQTT
collect_registrations() {
    local tmpdir="$1"
    local reg_file="${tmpdir}/registrations.jsonl"

    echo -e "${CYAN}Scanning for pending registrations (${WAIT}s)...${NC}" >&2

    # Subscribe to per-device registration topics, collect for WAIT seconds
    # -v: print topic + payload, -W: timeout
    mosquitto_sub "${MQTT_ARGS[@]}" \
        -t "reefy/devices/bootstrap/+/register" \
        -v -W "${WAIT}" > "${reg_file}" 2>/dev/null || true

    # Parse results: each line is "topic payload"
    local count=0
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        # Split: topic is first word, rest is JSON payload
        local topic payload hostname
        topic=$(echo "$line" | awk '{print $1}')
        payload=$(echo "$line" | cut -d' ' -f2-)

        # Extract hostname from topic: reefy/devices/bootstrap/{hostname}/register
        hostname=$(echo "$topic" | awk -F'/' '{print $4}')

        if [[ -n "$hostname" ]] && echo "$payload" | jq . >/dev/null 2>&1; then
            count=$((count + 1))
            echo "${hostname}|${payload}" >> "${tmpdir}/devices.txt"
        fi
    done < "${reg_file}"

    echo "$count"
}

# Display pending devices
show_devices() {
    local devices_file="$1"
    local i=1

    echo ""
    echo -e "${BOLD}Pending devices:${NC}"
    echo ""

    while IFS='|' read -r hostname payload; do
        local mac timestamp ago
        mac=$(echo "$payload" | jq -r '.mac // "unknown"')
        timestamp=$(echo "$payload" | jq -r '.timestamp // 0')
        has_csr=$(echo "$payload" | jq -r 'if .csr then "yes" else "no" end')

        # Calculate time ago
        if [[ "$timestamp" != "0" ]] && [[ "$timestamp" != "null" ]]; then
            local now ts_int diff
            now=$(date +%s)
            ts_int=$(printf "%.0f" "$timestamp" 2>/dev/null || echo 0)
            diff=$((now - ts_int))
            if [[ $diff -lt 60 ]]; then
                ago="${diff}s ago"
            elif [[ $diff -lt 3600 ]]; then
                ago="$((diff / 60))m ago"
            else
                ago="$((diff / 3600))h ago"
            fi
        else
            ago="unknown"
        fi

        printf "  ${BOLD}%d.${NC} %-28s MAC=%-17s  %s  CSR=%s\n" \
            "$i" "$hostname" "$mac" "$ago" "$has_csr"
        i=$((i + 1))
    done < "$devices_file"
    echo ""
}

# Approve a device
approve_device() {
    local tmpdir="$1"
    local device_num="$2"
    local devices_file="${tmpdir}/devices.txt"

    # Get device info
    local line hostname payload
    line=$(sed -n "${device_num}p" "$devices_file")
    hostname=$(echo "$line" | cut -d'|' -f1)
    payload=$(echo "$line" | cut -d'|' -f2-)

    local has_csr
    has_csr=$(echo "$payload" | jq -r 'if .csr then "yes" else "no" end')

    if [[ "$has_csr" != "yes" ]]; then
        echo -e "${RED}Device ${hostname} has no CSR in registration. Cannot provision.${NC}"
        return 1
    fi

    echo -e "${CYAN}Signing CSR for ${hostname}...${NC}"

    # Extract CSR to temp file
    local csr_file="${tmpdir}/device.csr"
    echo "$payload" | jq -r '.csr' > "$csr_file"

    # Verify CSR is valid PEM
    if ! openssl req -in "$csr_file" -noout -verify 2>/dev/null; then
        echo -e "${RED}Invalid CSR from device ${hostname}${NC}"
        return 1
    fi

    # Sign CSR using generate-device-cert.sh
    local cert_json
    cert_json=$("${SCRIPT_DIR}/generate-device-cert.sh" \
        -r "$csr_file" \
        -c "$CERTS_DIR" \
        -j)

    local uuid
    uuid=$(echo "$cert_json" | jq -r '.uuid')

    echo -e "${GREEN}UUID: ${uuid}${NC}"

    # Publish provisioning response
    local provision_topic="reefy/devices/bootstrap/${hostname}/provision"
    mosquitto_pub "${MQTT_ARGS[@]}" \
        -t "$provision_topic" \
        -m "$cert_json" \
        -q 1

    echo -e "${GREEN}Certificate published to ${provision_topic}${NC}"

    # Clear the retained registration message (publish empty retained)
    local register_topic="reefy/devices/bootstrap/${hostname}/register"
    mosquitto_pub "${MQTT_ARGS[@]}" \
        -t "$register_topic" \
        -n -r

    echo -e "${GREEN}Device ${hostname} provisioned as ${uuid}${NC}"
    echo ""
}

# Main loop
main() {
    echo ""
    echo -e "${BOLD}Reefy Device Approval Tool${NC}"
    echo -e "Broker: ${HOST}:${PORT}"
    echo ""

    # Test broker connectivity
    if ! mosquitto_pub "${MQTT_ARGS[@]}" -t "reefy/devices/bootstrap/admin/ping" -m "ping" 2>/dev/null; then
        echo -e "${RED}Cannot connect to broker at ${HOST}:${PORT}${NC}" >&2
        exit 1
    fi

    while true; do
        local tmpdir
        tmpdir=$(mktemp -d)

        local count
        count=$(collect_registrations "$tmpdir")

        if [[ "$count" -eq 0 ]]; then
            echo -e "No pending registrations found."
            echo ""
        else
            show_devices "${tmpdir}/devices.txt"
        fi

        echo -e "[${BOLD}a${NC}]pprove  [${BOLD}r${NC}]efresh  [${BOLD}q${NC}]uit"
        read -r -p "> " action

        case "$action" in
            a|approve)
                if [[ "$count" -eq 0 ]]; then
                    echo "No devices to approve. Try [r]efresh."
                    rm -rf "$tmpdir"
                    continue
                fi

                read -r -p "Device number: " num
                if [[ "$num" =~ ^[0-9]+$ ]] && [[ "$num" -ge 1 ]] && [[ "$num" -le "$count" ]]; then
                    approve_device "$tmpdir" "$num"
                else
                    echo "Invalid device number."
                fi
                ;;
            r|refresh)
                echo ""
                ;;
            q|quit|exit)
                rm -rf "$tmpdir"
                echo "Bye."
                exit 0
                ;;
            *)
                echo "Unknown action: $action"
                ;;
        esac

        rm -rf "$tmpdir"
    done
}

main
