#!/bin/busybox sh
# Early and runtime recovery use the same authenticated copy of this script.
# Retain failed partition UUIDs until a healthy boot is confirmed, so two bad
# slots stop in recovery instead of switching forever.
set -eu
PATH=/sbin:/bin:/usr/sbin:/usr/bin
export PATH
VARIABLE=/sys/firmware/efi/efivars/ReefyVerityFailed-5d9f8b18-76d4-4d04-92e6-e93d6fabab91

stop_boot() {
    echo "[reefy-verity] recovery stopped: $*" > /dev/kmsg
    # No unverified shell or root fallback. Preserve the serial diagnostic.
    while :; do sleep 3600; done
}

mkdir -p /sys/firmware/efi/efivars
mountpoint -q /sys/firmware/efi/efivars || \
    mount -t efivarfs efivarfs /sys/firmware/efi/efivars || stop_boot 'no EFI variables'
mount -o remount,rw /sys/firmware/efi/efivars || stop_boot 'EFI variables are read-only'

if [ "${1:-}" = clear ]; then
    if [ -e "$VARIABLE" ]; then
        chattr -i "$VARIABLE"
        rm "$VARIABLE"
    fi
    mount -o remount,ro /sys/firmware/efi/efivars
    exit 0
fi

info=$(efibootmgr -v) || stop_boot 'cannot read boot entries'
if [ -s /boot-guid ]; then
    # BootCurrent numbers can be recycled by entry repair after startup.
    guid=$(cat /boot-guid)
else
    . /verity-boot-guid.sh
    guid=$(verity_boot_guid) || stop_boot 'cannot identify UKI partition'
fi
case "$guid" in ''|*[!0-9a-f-]*) stop_boot 'invalid current partition' ;; esac
# USB and other built-in storage drivers may still be probing after Linux
# starts. Wait only for the identified partition, without mounting or writing it.
tries=0
device=''
while [ "$tries" -lt 30 ]; do
    device=$(blkid -t "PARTUUID=$guid" -o device 2>/dev/null) || device=''
    [ ! -b "$device" ] || break
    sleep 1
    tries=$((tries + 1))
done
[ -b "$device" ] || stop_boot 'current partition missing or ambiguous'
parent=$(dirname "$(readlink -f "/sys/class/block/${device##*/}")")
failed=''
if [ -e "$VARIABLE" ]; then
    failed=$(dd if="$VARIABLE" bs=1 skip=4 2>/dev/null)
    case "$failed" in *[!0-9a-f,-]*) stop_boot 'invalid recovery history' ;; esac
fi
case ",$failed," in *",$guid,"*) stop_boot 'slot already failed' ;; esac
failed="${failed:+$failed,}$guid"
[ "${#failed}" -le 73 ] || stop_boot 'both slots failed'
[ ! -e "$VARIABLE" ] || chattr -i "$VARIABLE" || stop_boot 'cannot unlock recovery history'
printf '\007\000\000\000%s' "$failed" > "$VARIABLE" || stop_boot 'cannot persist recovery history'
[ "$(dd if="$VARIABLE" bs=1 skip=4 2>/dev/null)" = "$failed" ] || stop_boot 'recovery history readback failed'

# Only select a Reefy A/B entry on another partition of the same physical disk.
# Resolve from actual GPT identity rather than assuming stable Boot numbers.
target=''
entries=$(printf '%s\n' "$info" | grep -E '^Boot[0-9A-Fa-f]{4}\*?[[:space:]]+reefy-[ab][[:space:]]' || true)
while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    other=$(printf '%s\n' "$candidate" | sed -nE 's/.*HD\([0-9]+,GPT,([^,]+),.*/\1/p' | tr A-Z a-z)
    case "$other" in ''|*[!0-9a-f-]*) continue ;; esac
    case ",$failed," in *",$other,"*) continue ;; esac
    other_device=$(blkid -t "PARTUUID=$other" -o device) || continue
    [ -b "$other_device" ] || continue
    [ "$(dirname "$(readlink -f "/sys/class/block/${other_device##*/}")")" = "$parent" ] || continue
    number=$(printf '%s\n' "$candidate" | cut -c5-8)
    [ -z "$target" ] || stop_boot 'ambiguous alternate entry'
    target=$number
done <<EOF
$entries
EOF
[ -n "$target" ] || stop_boot 'no untried alternate ESP'
efibootmgr -n "$target" || stop_boot 'cannot set alternate BootNext'
[ "$(efibootmgr | sed -n 's/^BootNext: //p')" = "$target" ] || stop_boot 'BootNext readback failed'
echo "[reefy-verity] selecting alternate ESP after verification failure" > /dev/kmsg
sync
mount -o remount,ro /sys/firmware/efi/efivars
reboot -f
stop_boot 'reboot returned'
