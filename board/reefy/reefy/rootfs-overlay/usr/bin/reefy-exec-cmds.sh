#!/bin/sh
# Execute user-provided reefy-cmds.sh from USB or VMware shared folder
for path in /mnt/reefy/reefy-cmds.sh /mnt/vmware/reefy-cmds.sh; do
    if [ -f "${path}" ]; then
        echo "[reefy] Executing commands from ${path}"
        sh "${path}" || echo "[reefy] Warning: ${path} exited with error $?"
        exit 0
    fi
done
echo "[reefy] No reefy-cmds.sh found"
