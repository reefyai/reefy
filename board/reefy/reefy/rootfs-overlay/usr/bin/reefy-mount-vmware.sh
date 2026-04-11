#!/bin/sh
# Mount VMware shared folder named "reefy"
VMWARE_MNT="/mnt/vmware"
mkdir -p "${VMWARE_MNT}"
vmhgfs-fuse .host:/reefy "${VMWARE_MNT}" -o allow_other || {
    echo "[reefy] Failed to mount VMware shared folder"
    exit 0
}
echo "[reefy] VMware shared folder mounted at ${VMWARE_MNT}"
