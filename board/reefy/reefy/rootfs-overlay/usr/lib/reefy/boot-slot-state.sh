# Private shared identity check for the boot confirmation and rollback helpers.
# BootCurrent records the entry number at firmware startup. Entry repair can
# delete and reuse that number, so only the mounted partition's GUID is stable.
# Return 0 for the default slot, 1 for another slot, and 2 for unknown identity.
mounted_slot_is_default() (
    SLOT_SOURCE=$(findmnt -rn -o SOURCE /mnt/reefy 2>/dev/null) || return 2
    [ -n "$SLOT_SOURCE" ] || return 2
    SLOT_GUID=$(blkid -s PARTUUID -o value "$SLOT_SOURCE" 2>/dev/null) || return 2
    case "$SLOT_GUID" in ''|*[!a-zA-Z0-9-]*) return 2 ;; esac
    SLOT_EFI=$(efibootmgr -v 2>/dev/null) || return 2
    SLOT_FIRST=$(printf '%s\n' "$SLOT_EFI" | awk '/^BootOrder:/ {split($2,a,","); print a[1]; exit}')
    printf '%s\n' "$SLOT_FIRST" | grep -Eq '^[[:xdigit:]]{4}$' || return 2
    SLOT_ENTRY=$(printf '%s\n' "$SLOT_EFI" | grep -Ei "^Boot${SLOT_FIRST}\\*?[[:space:]]") || return 2
    [ "$(printf '%s\n' "$SLOT_ENTRY" | grep -c .)" -eq 1 ] || return 2
    SLOT_DEFAULT_GUID=$(printf '%s\n' "$SLOT_ENTRY" | sed -nE 's/.*HD\([0-9]+,GPT,([^,]+),.*/\1/p')
    case "$SLOT_DEFAULT_GUID" in ''|*[!a-zA-Z0-9-]*) return 2 ;; esac
    [ "$(printf '%s' "$SLOT_GUID" | tr A-Z a-z)" = "$(printf '%s' "$SLOT_DEFAULT_GUID" | tr A-Z a-z)" ]
)
