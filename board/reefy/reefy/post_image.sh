#!/bin/bash
set -euxo pipefail

# Place efi and raw images into buildroot/output/images dir
pushd ${BINARIES_DIR}

echo Building initramfs with squashfs rootfs inside
sudo -E "${BR2_EXTERNAL_REEFY_PATH}"/board/reefy/reefy/scripts/create_initramfs.sh

# TODO: avoid calling sudo
echo Building reefy.efi uefi uki image
sudo -E "${BR2_EXTERNAL_REEFY_PATH}"/board/reefy/reefy/scripts/create_efi.sh

echo Building reefy.raw bootable image
sudo -E "${BR2_EXTERNAL_REEFY_PATH}"/board/reefy/reefy/scripts/create_raw.sh

popd
