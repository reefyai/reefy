#!/bin/bash

set -euxo pipefail

# A/B partition layout:
#   Partition 1: ESP "sbnb-a" (1 GiB) — boot slot A
#   Partition 2: ESP "sbnb-b" (1 GiB) — boot slot B
#   Partitions 3+4 created on first boot by boot-sbnb-storage.sh:
#     Partition 3: Key partition (1 MiB, msftres) — LUKS passphrase
#     Partition 4: Data partition (rest of disk) — LUKS-encrypted f2fs

EFI=sbnb.efi
IMG_FILE=sbnb.raw
VHD_FILE=sbnb.vhd
TMP_DIR=$(mktemp -d)
FS_SIZE="2100" # in MB (2x 1GiB ESPs + slack)
SBNB_TSKEY="${BR2_EXTERNAL_SBNB_PATH}"/sbnb-tskey.txt

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root"
    exit 1
fi

dd if=/dev/zero of=${IMG_FILE} bs=1M count=${FS_SIZE}
LOOP=$(losetup --show -f ${IMG_FILE})
parted -s ${LOOP} mklabel gpt
parted -s ${LOOP} mkpart sbnb-a 1MiB 1025MiB
parted -s ${LOOP} set 1 boot on
parted -s ${LOOP} set 1 esp on
parted -s ${LOOP} mkpart sbnb-b 1025MiB 2049MiB
parted -s ${LOOP} set 2 esp on

partprobe ${LOOP}

mkfs.vfat -F 32 -n sbnb-a ${LOOP}p1
mkfs.vfat -F 32 -n sbnb-b ${LOOP}p2

# Write identical content to both ESPs
for part in ${LOOP}p1 ${LOOP}p2; do
    mount -o loop ${part} ${TMP_DIR}
    mkdir -p ${TMP_DIR}/EFI/Boot/
    cp ${EFI} ${TMP_DIR}/EFI/Boot/bootx64.efi
    if [ -e ${SBNB_TSKEY} ]; then
        cp ${SBNB_TSKEY} ${TMP_DIR}/
    fi
    umount ${TMP_DIR}
done

rm -rf ${TMP_DIR}
losetup -d ${LOOP}

zip ${IMG_FILE}.zip ${IMG_FILE}
zip ${EFI}.zip ${EFI}

qemu-img convert -f raw -O vpc ${IMG_FILE} ${VHD_FILE}

echo Raw sbnb image for bare metal is ${IMG_FILE}
echo VHD sbnb image is ${VHD_FILE}
