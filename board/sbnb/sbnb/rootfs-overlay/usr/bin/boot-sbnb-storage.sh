#!/bin/sh
set -euxo pipefail

# Phase 1: Critical path — storage setup.
# Must complete before Docker, MQTT, and other services can start.
#
# A/B partition layout:
#   p1: ESP "sbnb-a" (1 GiB)
#   p2: ESP "sbnb-b" (1 GiB)
#   p3: Key partition (1 MiB, msftres) — LUKS passphrase  [created during adoption]
#   p4: Data partition (rest of disk) — LUKS-encrypted f2fs [created during adoption]
#
# This script only discovers and mounts existing storage.
# Partition creation happens in the reconciler during adoption.
# If no persistent storage exists (fresh USB), tmpfs is used for bootstrap.

SBNB_MNT="/mnt/sbnb"
SBNB_DATA_MNT="/mnt/sbnb-data"
LUKS_NAME="sbnb-data"
LUKS_KEY_SIZE=44

set_hostname() {
    hostname "$(sbnb-default-hostname)"
}

# Determine active boot slot from UEFI BootCurrent
get_active_slot() {
    CURRENT_BOOTNUM=$(efibootmgr 2>/dev/null | grep '^BootCurrent:' | awk '{print $2}')
    ACTIVE_LABEL=$(efibootmgr 2>/dev/null | grep "^Boot${CURRENT_BOOTNUM}\*" | \
        sed 's/Boot[0-9A-F]*\* //' | awk '{print $1}')

    if [ "$ACTIVE_LABEL" = "sbnb-a" ]; then
        INACTIVE_LABEL="sbnb-b"
    elif [ "$ACTIVE_LABEL" = "sbnb-b" ]; then
        INACTIVE_LABEL="sbnb-a"
    else
        # Booted from a UEFI auto-created entry (not sbnb-a/sbnb-b).
        # Determine which partition by checking the verbose entry for partition GUID.
        SBNB_A_DEV=$(blkid -L sbnb-a 2>/dev/null) || true
        SBNB_B_DEV=$(blkid -L sbnb-b 2>/dev/null) || true
        if [ -n "$SBNB_A_DEV" ] || [ -n "$SBNB_B_DEV" ]; then
            GUID_A=$(blkid -s PARTUUID -o value "$SBNB_A_DEV" 2>/dev/null) || true
            GUID_B=$(blkid -s PARTUUID -o value "$SBNB_B_DEV" 2>/dev/null) || true
            ENTRY_DETAIL=$(efibootmgr -v 2>/dev/null | grep "^Boot${CURRENT_BOOTNUM}\*")
            if [ -n "$GUID_A" ] && echo "$ENTRY_DETAIL" | grep -qi "$GUID_A" 2>/dev/null; then
                ACTIVE_LABEL="sbnb-a"
                INACTIVE_LABEL="sbnb-b"
                echo "[sbnb] Mapped Boot${CURRENT_BOOTNUM} to sbnb-a (by partition GUID)"
            elif [ -n "$GUID_B" ] && echo "$ENTRY_DETAIL" | grep -qi "$GUID_B" 2>/dev/null; then
                ACTIVE_LABEL="sbnb-b"
                INACTIVE_LABEL="sbnb-a"
                echo "[sbnb] Mapped Boot${CURRENT_BOOTNUM} to sbnb-b (by partition GUID)"
            else
                ACTIVE_LABEL=""
                INACTIVE_LABEL=""
            fi
        else
            # Legacy single-ESP layout (no A/B partitions)
            ACTIVE_LABEL=""
            INACTIVE_LABEL=""
        fi
    fi
}

# Mount the active ESP (the slot we booted from)
mount_sbnb_usb() {
    get_active_slot

    if [ -n "${ACTIVE_LABEL}" ]; then
        SBNB_DEV=$(blkid -L "${ACTIVE_LABEL}" 2>/dev/null) || true
    else
        # Fallback: try A/B labels, then legacy single-ESP label "sbnb"
        SBNB_DEV=$(blkid -L "sbnb-a" 2>/dev/null || blkid -L "sbnb-b" 2>/dev/null || blkid -L "sbnb" 2>/dev/null) || true
    fi

    if [ -n "${SBNB_DEV}" ]; then
        mkdir -p "${SBNB_MNT}" || true
        mount -o ro "${SBNB_DEV}" "${SBNB_MNT}" || true
        echo "[sbnb] Mounted ${SBNB_DEV} (${ACTIVE_LABEL:-first-boot}) at ${SBNB_MNT}"
    else
        echo "[sbnb] No device with label sbnb-a or sbnb-b found."
    fi
}

# Ensure UEFI boot entries exist for both A/B partitions (idempotent).
# Also removes duplicate auto-created entries from UEFI firmware.
ensure_boot_entries() {
    sbnb-efi fix
}

# Create/mount encrypted data partition
# Key = partition 3, Data = partition 4 (A/B layout)
setup_data_partition() {
    [ -z "${SBNB_DEV}" ] && return 0

    if mountpoint -q "${SBNB_DATA_MNT}" 2>/dev/null; then
        echo "[sbnb] ${SBNB_DATA_MNT} already mounted"
        return 0
    fi

    PARENT_NAME=$(lsblk -no PKNAME "${SBNB_DEV}" 2>/dev/null)
    [ -z "${PARENT_NAME}" ] && return 0
    SBNB_DISK="/dev/${PARENT_NAME}"
    KEY_PART="${SBNB_DISK}3"
    DATA_PART="${SBNB_DISK}4"

    mkdir -p "${SBNB_DATA_MNT}"
    modprobe dm_crypt 2>/dev/null || true

    # If LUKS partition exists, try to open and mount
    if [ -b "${DATA_PART}" ]; then
        if cryptsetup isLuks "${DATA_PART}" 2>/dev/null; then
            if cryptsetup luksOpen "${DATA_PART}" "${LUKS_NAME}" \
                --key-file "${KEY_PART}" --keyfile-size "${LUKS_KEY_SIZE}" 2>/dev/null; then
                FS_TYPE=$(blkid -o value -s TYPE "/dev/mapper/${LUKS_NAME}" 2>/dev/null)
                case "${FS_TYPE}" in
                    f2fs)  MOUNT_OPTS="noatime" ;;
                    *)     MOUNT_OPTS="noatime,commit=60" ;;
                esac
                if mount -o "${MOUNT_OPTS}" "/dev/mapper/${LUKS_NAME}" "${SBNB_DATA_MNT}" 2>/dev/null; then
                    echo "[sbnb] Mounted USB data partition (${FS_TYPE}) at ${SBNB_DATA_MNT}"
                    return 0
                fi
            else
                echo "[sbnb] USB data partition key mismatch, skipping"
            fi
        fi
    fi

    # Fallback: ensure state dir exists on rootfs overlay (already in RAM).
    # No mount needed — rootfs is writable overlay. Ephemeral until adoption
    # creates real persistent storage.
    mkdir -p "${SBNB_DATA_MNT}/state/lan"
    echo "[sbnb] No persistent storage, using rootfs overlay for bootstrap"
}

STORAGE_VG="sbnb"
STORAGE_LV="data"

# Try to use internal drive as /mnt/sbnb-data instead of slow USB.
# Opens LUKS on all internal drives with our key, activates LVM VG,
# then mounts the LV directly as /mnt/sbnb-data (replacing USB mount).
# Runs independently — does not require USB p4 to be mounted.
# If no internal drive found, USB stays mounted (fallback).
setup_internal_storage() {
    [ -z "${SBNB_DEV}" ] && return 0

    PARENT_NAME=$(lsblk -no PKNAME "${SBNB_DEV}" 2>/dev/null)
    [ -z "${PARENT_NAME}" ] && return 0
    KEY_PART="/dev/${PARENT_NAME}3"
    [ ! -b "${KEY_PART}" ] && return 0

    modprobe dm_crypt 2>/dev/null || true
    modprobe dm_mod 2>/dev/null || true

    # Open LUKS on all internal drives with our key
    for dev in $(lsblk -dpno NAME 2>/dev/null); do
        [ "${dev}" = "/dev/${PARENT_NAME}" ] && continue
        cryptsetup isLuks "${dev}" 2>/dev/null || continue
        luks_name="sbnb-$(basename ${dev})"
        [ -e "/dev/mapper/${luks_name}" ] && continue
        cryptsetup luksOpen "${dev}" "${luks_name}" \
            --key-file "${KEY_PART}" --keyfile-size "${LUKS_KEY_SIZE}" 2>/dev/null || continue
        echo "[sbnb] Opened LUKS on ${dev}"
    done

    # Scan for LVM and activate VG
    vgscan >/dev/null 2>&1
    vgs "${STORAGE_VG}" >/dev/null 2>&1 || return 0
    vgchange -ay "${STORAGE_VG}" >/dev/null 2>&1
    lv_path="/dev/${STORAGE_VG}/${STORAGE_LV}"
    [ ! -e "${lv_path}" ] && return 0

    # Mount internal drive as /mnt/sbnb-data
    echo "[sbnb] Internal drive found, mounting as ${SBNB_DATA_MNT}..."
    mkdir -p "${SBNB_DATA_MNT}"
    mount -o noatime,commit=60 "${lv_path}" "${SBNB_DATA_MNT}" || {
        echo "[sbnb] WARNING: Internal drive mount failed"
        return 0
    }

    # Ensure required directories exist (first time on internal drive)
    mkdir -p "${SBNB_DATA_MNT}/state/lan"
    mkdir -p "${SBNB_DATA_MNT}/apps"
    mkdir -p "${SBNB_DATA_MNT}/docker"

    echo "[sbnb] Mounted internal drive as ${SBNB_DATA_MNT}"
}

# Main execution
# Order matters: try internal drive first (fast), fall back to USB p4 (slow).
# If neither exists (fresh USB), use tmpfs for bootstrap.
set_hostname
mount_sbnb_usb

if ! mountpoint -q "${SBNB_MNT}" 2>/dev/null; then
    echo "[sbnb] WARNING: ${SBNB_MNT} not mounted — USB dongle may need re-flashing with A/B image"
    mkdir -p "${SBNB_DATA_MNT}/state/lan"
    exit 0
fi

ensure_boot_entries
setup_internal_storage
setup_data_partition
