#!/bin/busybox sh
# Runs from the retained initramfs tools, with no rootfs executable dependency.
PATH=/sbin:/bin:/usr/sbin:/usr/bin
export PATH
/bin/busybox --install
while :; do
    status=$(dmsetup status --noflush reefy-root) || exec sh /verity-recover.sh
    case "$status" in
        *' verity V'*) ;;
        *) exec sh /verity-recover.sh ;;
    esac
    sleep 1
done
