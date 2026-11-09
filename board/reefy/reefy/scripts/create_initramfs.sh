#!/bin/bash

set -euxo pipefail

INITRAMFS="${BINARIES_DIR}/rootfs-reefy.cpio"
INITRAMFS_DIR="${BINARIES_DIR}/rootfs-reefy"

# Create initramfs structure
rm -rf "${INITRAMFS_DIR}"
mkdir -p "${INITRAMFS_DIR}"
mkdir -p "${INITRAMFS_DIR}/bin"
mkdir -p "${INITRAMFS_DIR}/sbin"
mkdir -p "${INITRAMFS_DIR}/usr/bin"
mkdir -p "${INITRAMFS_DIR}/usr/sbin"

# Copy statically linked busybox into initramfs
cp "${TARGET_DIR}/bin/busybox" "${INITRAMFS_DIR}/bin"

# Generate the complete protected payload before assembling/signing the UKI.
SCRIPTS="${BR2_EXTERNAL_REEFY_PATH}/board/reefy/reefy/scripts"
mkdir -p "${INITRAMFS_DIR}/etc"
python3 "${SCRIPTS}/create_verity.py" \
    "${BINARIES_DIR}/rootfs.squashfs" \
    "${INITRAMFS_DIR}/rootfs.squashfs.verity" \
    "${INITRAMFS_DIR}/etc/veritytab" \
    --veritysetup "${HOST_DIR}/sbin/veritysetup" \
    --metrics "${BINARIES_DIR}/verity-build-metrics.json"

# Host readelf inspects target ELF dependencies without executing target code.
python3 "${SCRIPTS}/copy_initramfs_tools.py" "${TARGET_DIR}" "${INITRAMFS_DIR}" \
    --readelf readelf /sbin/dmsetup /usr/sbin/efibootmgr /sbin/blkid \
    /usr/lib/systemd/systemd-veritysetup
cp "${SCRIPTS}/verity-init.sh" "${SCRIPTS}/verity-recover.sh" \
    "${SCRIPTS}/verity-monitor.sh" "${SCRIPTS}/verity-boot-guid.sh" "${INITRAMFS_DIR}/"

# Place init script
cp "${BR2_EXTERNAL_REEFY_PATH}/board/reefy/reefy/scripts/init" "${INITRAMFS_DIR}"

# cpio initramfs
pushd "${INITRAMFS_DIR}"
find . -print0 | LC_ALL=C sort -z | cpio --null -ov --reproducible --format=newc > "${INITRAMFS}"
popd

echo Output: "${INITRAMFS}"
