#!/bin/sh
# Mount VMware shared folder named "sbnb"
VMWARE_MNT="/mnt/vmware"
mkdir -p "${VMWARE_MNT}"
vmhgfs-fuse .host:/sbnb "${VMWARE_MNT}" -o allow_other || {
    echo "[sbnb] Failed to mount VMware shared folder"
    exit 0
}
echo "[sbnb] VMware shared folder mounted at ${VMWARE_MNT}"
