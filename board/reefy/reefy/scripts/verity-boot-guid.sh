#!/bin/busybox sh
# Identify the partition that supplied the UKI, independent of recycled boot
# numbers and firmware whole-disk fallback entries without an HD() node.
verity_valid_guid() {
    case "$1" in *[!0-9a-f-]*) return 1 ;; esac
    case "$1" in ????????-????-????-????-????????????) ;; *) return 1 ;; esac
    compact=$(printf '%s' "$1" | tr -d '-')
    [ "${#compact}" -eq 32 ]
}

verity_boot_guid() {
    variables=${1:-/sys/firmware/efi/efivars}
    for name in StubDevicePartUUID LoaderDevicePartUUID; do
        file="$variables/$name-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
        [ -f "$file" ] || continue
        # Standard systemd EFI variables contain a UTF-16 GUID after the
        # four-byte efivarfs attributes. GUID characters are ASCII only.
        guid=$(dd if="$file" bs=1 skip=4 2>/dev/null | tr -d '\000' | tr A-Z a-z)
        if verity_valid_guid "$guid"; then
            printf '%s\n' "$guid"
            return 0
        fi
    done
    info=$(efibootmgr -v) || return 1
    current=$(printf '%s\n' "$info" | sed -n 's/^BootCurrent: //p')
    case "$current" in *[!0-9A-Fa-f]*|'') return 1 ;; esac
    [ "${#current}" -eq 4 ] || return 1
    entry=$(printf '%s\n' "$info" | grep -E "^Boot$current\\*?[[:space:]]") || return 1
    guid=$(printf '%s\n' "$entry" | sed -nE 's/.*HD\([0-9]+,GPT,([^,]+),.*/\1/p' | tr A-Z a-z)
    verity_valid_guid "$guid" || return 1
    printf '%s\n' "$guid"
}
