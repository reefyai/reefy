#!/bin/bash
set -euo pipefail

# Buildroot overlays are additive. Remove known development-only units from
# a reused target only when the current source overlay no longer ships them.
# Never run this against a device filesystem or prune package-owned files.
overlay=${1:?source overlay required}
target=${2:?build target required}
test -d "$overlay/usr/lib/reefy"
test -d "$target/usr/lib/reefy"
test "$target" != /
test "$target" != "$overlay"

for relative in \
    etc/systemd/system/docker.service.d/storage-guard.conf \
    usr/lib/systemd/system/reefy-storage-activate.service \
    usr/lib/systemd/system/reefy-storage-drain.service \
    usr/lib/systemd/system/reefy-storage-guard.service \
    usr/lib/systemd/system/reefy-storage-hold.service \
    usr/lib/systemd/system/reefy-storage-images.service \
    usr/lib/systemd/system/reefy-storage-recover.service \
    usr/lib/systemd/system/reefy-storage-snapshot.service \
    usr/lib/systemd/system/reefy-storage-watchdog.service; do
    if [ ! -e "$overlay/$relative" ] && [ ! -L "$overlay/$relative" ]; then
        rm -f -- "$target/$relative"
        if [[ "$relative" == usr/lib/systemd/system/*.service ]] \
            && [ -d "$target/etc/systemd/system" ]; then
            # systemd presets can also leave generated enablement symlinks.
            find "$target/etc/systemd/system" -type l \
                -name "${relative##*/}" -delete
        fi
    fi
done
