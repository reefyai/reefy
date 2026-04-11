#!/bin/bash
#
# Reefy MQTT Local Test Environment
#
# Interactive tool that orchestrates the full MQTT provisioning flow:
# generate certs, start broker, inject config, approve devices, send playbooks.
#
# Usage:
#   ./tools/mqtt/full-mqtt-local-test.sh [-d BROKER_IP] [-i IMAGE]
#
# Options:
#   -d, --domain IP/HOST    Broker IP/hostname (default: auto-detect)
#   -i, --image FILE        Disk image path (default: buildroot/output/images/reefy.raw)
#   -h, --help              Show this help message
#
# All state is stored in tools/mqtt/mqtt-server/
#

set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
PROJECT_ROOT=$( cd -- "${SCRIPT_DIR}/../.." &> /dev/null && pwd )

# Work from tools/mqtt/ so sibling scripts and mqtt-server/ paths work naturally
cd "${SCRIPT_DIR}"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# Defaults
BROKER_IP=""
IMAGE=""
MQTT_DIR="./mqtt-server"
CERTS_DIR="${MQTT_DIR}/certs"
BROKER_DIR="${MQTT_DIR}/broker-config"

# Auto-detect local IP
detect_local_ip() {
    ip -4 route get 1 2>/dev/null | awk '{print $7; exit}' 2>/dev/null \
        || hostname -I 2>/dev/null | awk '{print $1}' \
        || echo "localhost"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--domain)
            BROKER_IP="$2"
            shift 2
            ;;
        -i|--image)
            IMAGE="$2"
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

# Set defaults after arg parsing
if [[ -z "${BROKER_IP}" ]]; then
    BROKER_IP=$(detect_local_ip)
fi

if [[ -z "${IMAGE}" ]]; then
    IMAGE="${PROJECT_ROOT}/buildroot/output/images/reefy.raw"
fi

# Check basic dependencies
for cmd in docker jq openssl; do
    if ! command -v "$cmd" &>/dev/null; then
        echo -e "${RED}Required: $cmd${NC}" >&2
        exit 1
    fi
done

# Helper: check if broker is running
broker_running() {
    if [[ -f "${BROKER_DIR}/docker-compose.yml" ]]; then
        docker compose -f "${BROKER_DIR}/docker-compose.yml" ps --status running 2>/dev/null | grep -q emqx
        return $?
    fi
    return 1
}

# Helper: broker status string
broker_status() {
    if broker_running; then
        echo -e "${GREEN}running${NC}"
    else
        echo -e "${DIM}stopped${NC}"
    fi
}

# Helper: check if certs exist
certs_exist() {
    [[ -f "${CERTS_DIR}/ca.crt" ]]
}

# Helper: list provisioned device UUIDs
# generate-device-cert.sh saves to ./device-certs/{UUID}/ by default
list_device_uuids() {
    local uuids=()
    for search_dir in "./device-certs" "${MQTT_DIR}/device-certs"; do
        if [[ -d "${search_dir}" ]]; then
            for dir in "${search_dir}"/*/; do
                [[ -d "$dir" ]] || continue
                local uuid
                uuid=$(basename "$dir")
                [[ "$uuid" != "*" ]] && uuids+=("$uuid")
            done
        fi
    done
    echo "${uuids[@]}"
}

# Action: Setup
do_setup() {
    echo ""
    if certs_exist; then
        echo -e "${YELLOW}Certs already exist in ${CERTS_DIR}${NC}"
        read -r -p "Regenerate? [y/N] " regen
        if [[ "${regen}" != "y" && "${regen}" != "Y" ]]; then
            # Stop stale broker if running (may have wrong volume mounts)
            if docker ps -q -f name=reefy-mqtt-broker &>/dev/null; then
                docker stop reefy-mqtt-broker &>/dev/null || true
                docker rm reefy-mqtt-broker &>/dev/null || true
            fi
            # Start broker with correct paths
            echo -e "${CYAN}Starting broker...${NC}"
            docker compose -f "${BROKER_DIR}/docker-compose.yml" up -d
            echo -e "${GREEN}Broker is running${NC}"
            return
        fi
    fi

    echo -e "${CYAN}Generating certs and configuring broker...${NC}"
    ./setup-mqtt-server.sh -d "${BROKER_IP}" -o mqtt-server

    echo ""
    echo -e "${CYAN}Starting broker...${NC}"
    docker compose -f "${BROKER_DIR}/docker-compose.yml" up -d

    echo ""
    echo -e "${GREEN}Setup complete${NC}"
    echo -e "  Dashboard: http://localhost:18083 (admin/public)"
}

# Action: Inject
do_inject() {
    echo ""
    if [[ ! -f "${IMAGE}" ]]; then
        echo -e "${RED}Image not found: ${IMAGE}${NC}"
        echo -e "Build with: ${CYAN}cd buildroot && make -j \$(nproc)${NC}"
        return
    fi

    if ! certs_exist; then
        echo -e "${RED}No certs found. Run [1] Setup first.${NC}"
        return
    fi

    echo -e "${CYAN}Injecting MQTT config into image...${NC}"
    echo -e "${DIM}Image: ${IMAGE}${NC}"
    echo ""

    if [[ $(id -u) -ne 0 ]]; then
        echo -e "${YELLOW}Requires root. Running with sudo...${NC}"
        sudo "${SCRIPT_DIR}/inject-mqtt-config.sh" -o mqtt-server -i "${IMAGE}"
    else
        ./inject-mqtt-config.sh -o mqtt-server -i "${IMAGE}"
    fi

    echo ""
    echo -e "${GREEN}Config injected${NC}"
    echo ""
    echo -e "Boot the device in a separate terminal:"
    echo -e "  ${CYAN}./scripts/reefy-local-boot.sh${NC}"
}

# Action: Approve devices
do_approve() {
    echo ""
    if ! certs_exist; then
        echo -e "${RED}No certs found. Run [1] Setup first.${NC}"
        return
    fi

    if ! broker_running; then
        echo -e "${RED}Broker not running. Run [1] Setup first.${NC}"
        return
    fi

    # Check mosquitto tools
    if ! command -v mosquitto_sub &>/dev/null; then
        echo -e "${RED}Required: mosquitto_sub (install mosquitto-clients)${NC}"
        return
    fi

    ./approve-devices.sh -c "${CERTS_DIR}" -H "${BROKER_IP}"
}

# Action: Send playbook
do_playbook() {
    echo ""
    if ! certs_exist; then
        echo -e "${RED}No certs found. Run [1] Setup first.${NC}"
        return
    fi

    if ! broker_running; then
        echo -e "${RED}Broker not running. Run [1] Setup first.${NC}"
        return
    fi

    if ! command -v mosquitto_pub &>/dev/null; then
        echo -e "${RED}Required: mosquitto_pub (install mosquitto-clients)${NC}"
        return
    fi

    # Find provisioned devices
    local uuids
    uuids=$(list_device_uuids)

    if [[ -z "$uuids" ]]; then
        echo -e "${YELLOW}No provisioned devices found.${NC}"
        echo "Run [3] Approve to provision a device first."
        return
    fi

    local uuid_array=($uuids)
    local selected_uuid=""

    if [[ ${#uuid_array[@]} -eq 1 ]]; then
        selected_uuid="${uuid_array[0]}"
        echo -e "Device: ${BOLD}${selected_uuid}${NC}"
    else
        echo -e "${BOLD}Provisioned devices:${NC}"
        local i=1
        for uuid in "${uuid_array[@]}"; do
            echo -e "  ${BOLD}${i}.${NC} ${uuid}"
            i=$((i + 1))
        done
        echo ""
        read -r -p "Device number: " num
        if [[ "$num" =~ ^[0-9]+$ ]] && [[ "$num" -ge 1 ]] && [[ "$num" -le ${#uuid_array[@]} ]]; then
            selected_uuid="${uuid_array[$((num - 1))]}"
        else
            echo "Invalid selection."
            return
        fi
    fi

    # Choose playbook
    echo ""
    echo -e "  ${BOLD}1.${NC} test-playbook.yml (built-in test)"
    echo -e "  ${BOLD}2.${NC} Custom playbook path"
    echo ""
    read -r -p "Playbook [1]: " pb_choice

    local playbook_path="test-playbook.yml"
    if [[ "$pb_choice" == "2" ]]; then
        read -r -p "Path: " playbook_path
        if [[ ! -e "$playbook_path" ]]; then
            echo -e "${RED}Not found: ${playbook_path}${NC}"
            return
        fi
    fi

    echo ""
    ./send-playbook.sh -c "${CERTS_DIR}" -d "${selected_uuid}" -H "${BROKER_IP}" -w "${playbook_path}"
}

# Action: Send mcl config
do_mcl() {
    echo ""
    if ! certs_exist; then
        echo -e "${RED}No certs found. Run [1] Setup first.${NC}"
        return
    fi

    if ! broker_running; then
        echo -e "${RED}Broker not running. Run [1] Setup first.${NC}"
        return
    fi

    if ! command -v mosquitto_pub &>/dev/null; then
        echo -e "${RED}Required: mosquitto_pub (install mosquitto-clients)${NC}"
        return
    fi

    # Find provisioned devices
    local uuids
    uuids=$(list_device_uuids)

    if [[ -z "$uuids" ]]; then
        echo -e "${YELLOW}No provisioned devices found.${NC}"
        echo "Run [3] Approve to provision a device first."
        return
    fi

    local uuid_array=($uuids)
    local selected_uuid=""

    if [[ ${#uuid_array[@]} -eq 1 ]]; then
        selected_uuid="${uuid_array[0]}"
        echo -e "Device: ${BOLD}${selected_uuid}${NC}"
    else
        echo -e "${BOLD}Provisioned devices:${NC}"
        local i=1
        for uuid in "${uuid_array[@]}"; do
            echo -e "  ${BOLD}${i}.${NC} ${uuid}"
            i=$((i + 1))
        done
        echo ""
        read -r -p "Device number: " num
        if [[ "$num" =~ ^[0-9]+$ ]] && [[ "$num" -ge 1 ]] && [[ "$num" -le ${#uuid_array[@]} ]]; then
            selected_uuid="${uuid_array[$((num - 1))]}"
        else
            echo "Invalid selection."
            return
        fi
    fi

    # Choose mcl config
    echo ""
    echo -e "  ${BOLD}1.${NC} test-config.mcl (built-in test)"
    echo -e "  ${BOLD}2.${NC} Custom mcl path"
    echo ""
    read -r -p "Config [1]: " mcl_choice

    local mcl_path="test-config.mcl"
    if [[ "$mcl_choice" == "2" ]]; then
        read -r -p "Path: " mcl_path
        if [[ ! -e "$mcl_path" ]]; then
            echo -e "${RED}Not found: ${mcl_path}${NC}"
            return
        fi
    fi

    echo ""
    ./send-mcl.sh -c "${CERTS_DIR}" -d "${selected_uuid}" -H "${BROKER_IP}" -w "${mcl_path}"
}

# Action: Status
do_status() {
    echo ""
    echo -e "${BOLD}Broker:${NC}"
    if [[ -f "${BROKER_DIR}/docker-compose.yml" ]]; then
        docker compose -f "${BROKER_DIR}/docker-compose.yml" ps 2>/dev/null || echo "  Not configured"
    else
        echo "  Not configured. Run [1] Setup."
    fi

    echo ""
    echo -e "${BOLD}Provisioned devices:${NC}"
    local uuids
    uuids=$(list_device_uuids)
    if [[ -z "$uuids" ]]; then
        echo "  None"
    else
        for uuid in $uuids; do
            echo "  ${uuid}"
        done
    fi

    echo ""
    echo -e "${BOLD}Certs:${NC}"
    if certs_exist; then
        echo -e "  ${GREEN}Present${NC} (${CERTS_DIR}/)"
    else
        echo -e "  ${DIM}Not generated${NC}"
    fi

    echo ""
    echo -e "${BOLD}Image:${NC}"
    if [[ -f "${IMAGE}" ]]; then
        local size
        size=$(du -h "${IMAGE}" 2>/dev/null | awk '{print $1}')
        echo -e "  ${GREEN}${IMAGE}${NC} (${size})"
    else
        echo -e "  ${DIM}Not found: ${IMAGE}${NC}"
    fi
    echo ""
}

# Main menu loop
main() {
    while true; do
        echo ""
        echo -e "${BOLD}Reefy MQTT Local Test Environment${NC}"
        echo -e "================================="
        echo -e "Broker: ${CYAN}${BROKER_IP}:8883${NC}  Status: $(broker_status)"
        echo -e "Image:  ${DIM}${IMAGE}${NC}"
        echo ""
        echo -e "  [${BOLD}1${NC}] Setup    - Generate certs & start broker"
        echo -e "  [${BOLD}2${NC}] Inject   - Inject MQTT config into reefy.raw"
        echo -e "  [${BOLD}3${NC}] Approve  - Discover & approve pending devices"
        echo -e "  [${BOLD}4${NC}] Playbook - Send ansible playbook to a device"
        echo -e "  [${BOLD}5${NC}] MCL      - Send mgmt config to a device"
        echo -e "  [${BOLD}6${NC}] Status   - Show broker & device status"
        echo -e "  [${BOLD}q${NC}] Quit"
        echo ""
        read -r -p "> " action

        case "$action" in
            1|setup)    do_setup ;;
            2|inject)   do_inject ;;
            3|approve)  do_approve ;;
            4|playbook) do_playbook ;;
            5|mcl)      do_mcl ;;
            6|status)   do_status ;;
            q|quit|exit)
                echo "Bye."
                exit 0
                ;;
            "")
                ;;
            *)
                echo "Unknown action: $action"
                ;;
        esac
    done
}

main
