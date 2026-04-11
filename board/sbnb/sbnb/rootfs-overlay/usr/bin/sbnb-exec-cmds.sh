#!/bin/sh
# Execute user-provided sbnb-cmds.sh from USB or VMware shared folder
for path in /mnt/sbnb/sbnb-cmds.sh /mnt/vmware/sbnb-cmds.sh; do
    if [ -f "${path}" ]; then
        echo "[sbnb] Executing commands from ${path}"
        sh "${path}" || echo "[sbnb] Warning: ${path} exited with error $?"
        exit 0
    fi
done
echo "[sbnb] No sbnb-cmds.sh found"
