#!/bin/busybox sh
# Consume one standard veritytab entry for the embedded OS image. Native
# systemd-veritysetup/libcryptsetup own option parsing and kernel activation.
verity_read_config() {
    conf=/etc/veritytab
    payload=/rootfs.squashfs.verity
    [ -f "$conf" ] && [ -f "$payload" ] || return 1
    [ "$(wc -c < "$conf")" -le 4096 ] || return 1
    found=0
    while read -r v_name v_data v_hash v_root v_options v_extra || \
            [ -n "$v_name$v_data$v_hash$v_root$v_options$v_extra" ]; do
        case "$v_name" in ''|\#*) continue ;; esac
        [ "$found" -eq 0 ] || return 1
        [ "$v_name" = reefy-root ] && [ "$v_data" = "$payload" ] && \
            [ "$v_hash" = "$payload" ] && [ -z "$v_extra" ] || return 1
        [ "${#v_root}" -eq 64 ] || return 1
        case "$v_root" in *[!0-9a-f]*) return 1 ;; esac
        # This boot path owns fail-closed recovery. Never accept flags that
        # expose unchecked data or reboot before its loop guard can run.
        case ",$v_options," in
            *,ignore-corruption,*|*,ignore-zero-blocks,*|*,check-at-most-once,*|\
            *,restart-on-corruption,*|*,panic-on-corruption,*|*,restart-on-error,*|*,panic-on-error,*) return 1 ;;
        esac
        found=1
        root_hash=$v_root
        options=$v_options
    done < "$conf"
    [ "$found" -eq 1 ]
}

verity_activate() {
    verity_read_config || return 1
    /usr/lib/systemd/systemd-veritysetup attach reefy-root "$payload" "$payload" \
        "$root_hash" "$options" || return 1
    dmsetup mknodes reefy-root || return 1
    table=$(dmsetup table reefy-root) || return 1
    case "$table" in
        *ignore_corruption*|*ignore_zero_blocks*|*check_at_most_once*|\
        *restart_on_corruption*|*panic_on_corruption*|*restart_on_error*|*panic_on_error*) return 1 ;;
    esac
    # Check every protected block before any code from the OS is executed.
    read -r scan_start ignored < /proc/uptime
    dd if=/dev/mapper/reefy-root of=/dev/null bs=1048576 || return 1
    # FEC may repair a metadata read after recursive attempts have latched C.
    # Such a mapping cannot safely use V/C transitions for runtime monitoring.
    case "$(dmsetup status --noflush reefy-root)" in
        *' verity V'*) ;;
        *) return 1 ;;
    esac
    read -r scan_end ignored < /proc/uptime
    printf '[reefy-verity] verified root=%s scan_start=%s scan_end=%s\n' \
        "$root_hash" "$scan_start" "$scan_end" > /dev/kmsg
    mkdir -p /run/reefy-verity
    cp "$conf" /run/reefy-verity/veritytab || return 1
}
