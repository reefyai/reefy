# Hostname on reefy-os

The device's hostname is `reefy-<mac-no-colons>` where `<mac>` is the
MAC of the first physical NIC (sorted lexically). This hostname is the
default MQTT client-id + topic namespace for unadopted devices, so
it's what the backend sees first and what users see in the adoption
queue.

## Components

| File | Purpose |
|---|---|
| `/usr/bin/reefy-derive-hostname` | Computes the name (`reefy-<mac>` or `reefy-mid-<machine-id>` fallback) from current sysfs state. Stateless. |
| `/usr/bin/reefy-set-hostname.sh` | Calls `reefy-derive-hostname` in a poll loop, waits up to 120s for a real NIC, applies the result via `hostname`. Always exits 0. |
| `/usr/lib/systemd/system/reefy-hostname.service` | `Type=oneshot` wrapper around the setter. `TimeoutStartSec=150s`. |
| `reefy-mqtt.service` | `Wants=reefy-hostname.service` + `After=reefy-hostname.service`. This is what pulls the hostname service into the boot transaction. |

## Why it's a separate service (and not part of `boot-reefy-storage.sh`)

It used to live in `boot-reefy-storage.sh` (early critical path).
Problem: that script runs *before* virtio_net finishes probing. On
~25% of QEMU boots virtio_net took >30s and the script read an empty
MAC from `/sys/class/net/eth0/address`, then fell back to
`xxd -l6 -p /dev/random` — a random hostname that got baked into SSH
host-key comments and stuck forever (see 2026-04 investigation).

The fix was to decouple it: the setter polls for a real NIC and blocks
up to 120s. `reefy-mqtt.service` waits on it, so MQTT never registers
with `buildroot` or a stale value.

## Identifying "physical" NICs

`reefy-derive-hostname` reads `ID_BUS` from `udevadm info --query=env`
and keeps only interfaces where `ID_BUS` is in
`{pci, usb, platform, mmc, sdio}`. That's the same signal udev's
built-in `net_id` computes from the ancestor chain, so a virtio-net
device shows `ID_BUS=pci` (its transport) even though its immediate
subsystem is `virtio`. Virtual interfaces (loopback, veth, docker0,
bridges, tailscale) don't get `ID_BUS` set at all and are ignored.

Why not walk `/sys/class/net/<iface>/device/subsystem` directly? For
virtio-net that symlink resolves to `/sys/bus/virtio`, not
`/sys/bus/pci`. `udevadm` does the ancestor traversal for us.

## Multi-NIC determinism

On a device with multiple physical NICs, any of their MACs is a valid
"this device" identifier, so the script picks the lexically-first
interface. With persistent-network-name udev rules
(`enp0s2`, `enp3s0`, etc. — PCI-slot-derived), that ordering is
stable across reboots.

Edge case: USB ethernet dongle hotplugged post-boot. udev would add an
`enx<mac>`-style name, which sorts after `enp*`, so onboard NIC keeps
winning. Hostname doesn't flip.

Device identity on the backend is the persistent UUID at
`/mnt/reefy-data/state/device-uuid`, not the hostname — so if hostname
ever did flip, it'd be cosmetic (log attribution, SSH comment,
dashboard label), not an identity-split bug.

## Fallback behavior

```
physical NIC visible with MAC   →  reefy-525400abc123
no NIC after 120s timeout       →  reefy-mid-<first 12 hex of /etc/machine-id>
no NIC, no /etc/machine-id      →  hostname stays `buildroot`, loud log
```

`/etc/machine-id` is generated per-device by systemd on first boot
(buildroot ships it empty). The `mid-` prefix makes it obvious that
the name isn't MAC-derived.

## Worst-case boot timing

| Scenario | Time from reefy-hostname.service start |
|---|---|
| Real hardware (PCI NIC, <1s probe) | ~1s |
| QEMU virtio_net (up to ~55s probe) | ~55s |
| No NIC ever | 120s (script timeout) |

reefy-mqtt is the only service that depends on this today; everything
else picks up the hostname at its leisure. Getty prints `buildroot
login:` briefly while the service is blocking — cosmetic.

## Troubleshooting

Service ran but hostname is wrong:
```
systemctl status reefy-hostname.service
journalctl -u reefy-hostname.service
hostname
reefy-derive-hostname   # run it manually
```

Check which NIC it picked:
```
for i in /sys/class/net/*; do
    [ "$(basename $i)" = lo ] && continue
    echo "$(basename $i): $(cat $i/address) ID_BUS=$(udevadm info --query=env --path=$i | sed -n 's/^ID_BUS=//p')"
done
```

If `ID_BUS` is empty for what you thought was a physical NIC, the
script treats it as virtual and skips it — file an issue with the
output of the above.
