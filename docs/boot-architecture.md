# Boot Architecture

## Overview

The boot process is split into two phases via systemd units, enabling parallel startup and fault isolation. Storage setup is on the critical path; everything else runs concurrently once storage is ready.

## Systemd Unit Graph

```
                       sbnb-storage.service ──────────────────────────────────┐
                          (critical path)                                     │
                    ┌───────┬──────────────┬──────┬──────┬──────┐             │
                    │       │              │      │      │      │             │
               docker  sbnb-wifi-early  tunnel  cmds  mgmt  nvidia-cdi   sbnb-init
                          │                                              (non-critical)
                    network-online.target
                    (--any, 30s timeout)
                          │
                      sbnb-mqtt
```

All services in the bottom row start **in parallel** once `sbnb-storage.service` completes.

`sbnb-wifi-early` runs before `network-online.target`, ensuring WiFi connectivity is available even without ethernet. `systemd-networkd-wait-online` uses `--any` so it succeeds as soon as any interface (WiFi or ethernet) has an IP address.

## Phase 1: sbnb-storage.service (critical path)

**Script**: `/usr/bin/boot-sbnb-storage.sh`
**Type**: oneshot, `RemainAfterExit=yes`
**Flags**: `set -euxo pipefail` — any failure here is fatal (no storage = no system)

| Step | Function | What it does | Failure impact |
|------|----------|-------------|----------------|
| 1 | `set_hostname` | Set hostname from MAC via `sbnb-default-hostname` | Cosmetic only |
| 2 | `mount_sbnb_usb` | Mount active ESP read-only at `/mnt/sbnb` | No certs, no MQTT config |
| 3 | `ensure_boot_entries` | Verify/fix EFI boot entries via `sbnb-efi fix` | Stale boot entries |
| 4 | `setup_internal_storage` | Open LUKS on internal drives, activate LVM VG `sbnb`, mount at `/mnt/sbnb-data` | Falls back to USB |
| 5 | `setup_data_partition` | If no internal storage: open LUKS on USB partition 4, mount at `/mnt/sbnb-data` | No persistent state |

**After this completes**: `/mnt/sbnb` and `/mnt/sbnb-data` are mounted. Docker, MQTT, and all other services can start.

## Phase 2: sbnb-init.service (non-critical, parallel)

**Script**: `/usr/bin/boot-sbnb-init.sh`
**Type**: oneshot, `RemainAfterExit=yes`
**Flags**: `set -uxo pipefail` — **no `set -e`**, errors are logged but don't stop execution
**After**: `sbnb-storage.service`

| Step | Function | What it does | Failure impact |
|------|----------|-------------|----------------|
| 1 | `setup_device_credentials` | Generate password (first boot), create `reefy` user, set root+reefy passwords via `mkpasswd`+`sed` | No SSH access, no credentials in dashboard |
| 2 | `display_banner` | Print ASCII art + IPs to `/dev/kmsg` | Cosmetic only |

**Nothing depends on this unit** — it runs alongside Docker/MQTT startup, not before it.

## All Systemd Units

| Unit | Type | Depends on | Purpose |
|------|------|-----------|---------|
| `sbnb-storage.service` | oneshot | `dev-disk-by-partlabel-sbnb.device` | Mount USB, open LUKS, mount internal storage |
| `sbnb-init.service` | oneshot | `sbnb-storage.service` | Device credentials, banner |
| `docker.service` | — | `sbnb-storage.service` | Container runtime (via drop-in `ramdisk.conf`) |
| `sbnb-wifi-early.service` | oneshot | `sbnb-storage.service` | Apply saved WiFi config before `network-online.target` |
| `sbnb-mqtt.service` | simple | `sbnb-storage.service`, `network-online.target` | MQTT reconciler — device registration, desired state |
| `sbnb-tunnel.service` | simple | `sbnb-storage.service` | Tailscale tunnel (if configured) |
| `sbnb-cmds.service` | oneshot | `sbnb-storage.service` | Execute custom commands from USB |
| `sbnb-mgmt.service` | simple | `sbnb-storage.service`, `network-online.target` | Mgmt config daemon (if configured) |
| `nvidia-cdi-generate.service` | oneshot | `sbnb-storage.service`, `systemd-modules-load` | GPU device nodes + CDI spec |
| `sbnb-watchdog.timer` | timer | — | Health check every 60s (starts 2min after boot) |

## Normal Boot Timeline

```
t=0   EFI hands off to systemd
t+1s  sbnb-storage.service starts
        ├── hostname set
        ├── USB mounted
        ├── EFI boot entries verified
        ├── internal drive mounted at /mnt/sbnb-data (or USB fallback)
        └── LUKS opened
t+3s  sbnb-storage.service completes → triggers parallel startup:
        ├── sbnb-wifi-early.service (applies saved WiFi config if present)
        ├── docker.service          (pulls images, starts containers)
        ├── sbnb-tunnel.service     (Tailscale tunnel)
        ├── nvidia-cdi-generate     (GPU device nodes)
        └── sbnb-init.service       (credentials + banner)
t+7s  sbnb-wifi-early completes (WiFi connected or skipped)
        └── network-online.target reached (--any: first interface with IP)
            └── sbnb-mqtt.service   (connects to EMQX, registers device)
t+9s  MQTT connected, device registered
t+10s Docker containers running
```

## First Boot Differences

On first boot, `sbnb-storage.service` takes longer (~5-10s extra):
- Creates USB partitions 2 (key) and 3 (data)
- Fills key partition with random data + passphrase
- LUKS formats partition 3
- Creates f2fs filesystem

Everything after storage setup is the same as normal boot.

## Fault Isolation

| Failure | Impact | Recovery |
|---------|--------|----------|
| `set_hostname` fails | Generic hostname, boot continues | Reboot |
| `mount_sbnb_usb` fails | Fatal — no USB means no certs/config | Check USB dongle |
| `setup_data_partition` fails | Fatal — no persistent state | Check disk, LUKS key |
| `setup_internal_storage` fails | Docker uses slow USB, boot continues | Check internal drive |
| `setup_device_credentials` fails | No SSH password, boot continues normally | Fixed on next reboot or manual setup |
| `display_banner` fails | No banner, boot continues | Cosmetic |
| GPU setup fails | No GPU passthrough, boot continues | Check nvidia modules |
 
## Implementation Files

| File | Purpose |
|------|---------|
| `usr/bin/boot-sbnb-storage.sh` | Phase 1: hostname, USB mount, EFI fix, LUKS, internal storage |
| `usr/bin/sbnb-efi` | Unified EFI boot entry management (fix/update/confirm/status) |
| `usr/bin/boot-sbnb-init.sh` | Phase 2: credentials, banner |
| `usr/lib/systemd/system/sbnb-storage.service` | Systemd unit for phase 1 |
| `usr/lib/systemd/system/sbnb-init.service` | Systemd unit for phase 2 |
| `usr/bin/sbnb-wifi-early` | Apply saved WiFi config from desired-state.json |
| `usr/lib/systemd/system/sbnb-wifi-early.service` | Systemd unit: runs after storage, before network-online |
| `usr/lib/systemd/system/systemd-networkd-wait-online.service.d/any-interface.conf` | Override: `--any --timeout=30` (succeed on first interface with IP) |
| `etc/systemd/system/docker.service.d/ramdisk.conf` | Docker depends on sbnb-storage |
