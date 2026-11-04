#!/bin/bash
set -euxo pipefail

# Ship only slot A. First boot creates an equally sized B before key/data slots.
EFI="${EFI:-reefy.efi}"
IMG_FILE="${IMG_FILE:-reefy.raw}"
VHD_FILE="${VHD_FILE:-reefy.vhd}"
SLOT_SIZE_MIB="${REEFY_SLOT_SIZE_MIB:-1024}"
[[ "$SLOT_SIZE_MIB" =~ ^[0-9]+$ ]] && (( SLOT_SIZE_MIB >= 64 ))
(( $(stat -c %s "$EFI") + 16 * 1024 * 1024 <= SLOT_SIZE_MIB * 1024 * 1024 ))
[[ "$EUID" = 0 ]]
TMP_DIR=$(mktemp -d)
LOOP=""
cleanup() {
    mountpoint -q "$TMP_DIR" && umount "$TMP_DIR" || true
    [[ -z "$LOOP" ]] || losetup -d "$LOOP"
    rmdir "$TMP_DIR"
}
trap cleanup EXIT
rm -f "$IMG_FILE"
truncate -s "$((SLOT_SIZE_MIB + 2))M" "$IMG_FILE"
LOOP=$(losetup --show -f "$IMG_FILE")
parted -s "$LOOP" mklabel gpt
parted -s "$LOOP" mkpart reefy-a 1MiB "$((SLOT_SIZE_MIB + 1))MiB"
parted -s "$LOOP" set 1 esp on
partprobe "$LOOP"
mkfs.vfat -F 32 -n reefy-a "${LOOP}p1"
mount "${LOOP}p1" "$TMP_DIR"
mkdir -p "$TMP_DIR/EFI/Boot"
cp "$EFI" "$TMP_DIR/EFI/Boot/bootx64.efi"
REEFY_TSKEY="${BR2_EXTERNAL_REEFY_PATH}/reefy-tskey.txt"
if [[ -e "$REEFY_TSKEY" ]]; then
    cp "$REEFY_TSKEY" "$TMP_DIR/"
fi
umount "$TMP_DIR"
qemu-img convert -f raw -O vpc "$IMG_FILE" "$VHD_FILE"
echo "Raw Reefy image: $IMG_FILE; VHD: $VHD_FILE"
