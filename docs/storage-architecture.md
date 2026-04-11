# Reefy Device Storage Architecture

## Overview

A reefy device has two mount points:

| Mount Point | Media | Purpose |
|-------------|-------|---------|
| `/mnt/sbnb` | USB dongle, partition 1 (A or B) | EFI image, certs, MQTT bootstrap (read-only) |
| `/mnt/sbnb-data` | Internal drive (preferred) or USB partition 4 (fallback) | Everything: device state, Docker, app volumes |

When an internal drive is available, it is mounted directly at `/mnt/sbnb-data` by `boot-sbnb-storage.sh`. If no internal drive, USB partition 4 is used as fallback.

## Partition Layout

### USB Dongle (boot device)

```
USB dongle (e.g., 58 GB)
├── Partition 1: EFI System Partition        (0 - 511 MiB)
│   Type:      EFI System (GPT)
│   Format:    VFAT, PARTLABEL="sbnb"
│   Mount:     /mnt/sbnb (read-only)
│   Contents:  EFI boot image, MQTT certs, bootstrap config
│   Lifecycle: Written once during image flash; updated only by firmware OTA
│
├── Partition 2: Key Partition               (511 - 512 MiB)
│   Type:      Microsoft Reserved (GPT type E3C9E316-...)
│   Format:    Raw binary (no filesystem)
│   Purpose:   LUKS passphrase storage (first 44 bytes); rest is random noise
│   Note:      Disguised as system partition — all OSes ignore this type
│   Lifecycle: Created on first boot; never modified after
│
└── Partition 3: Data Partition              (512 MiB - end of disk)
    Type:      Linux data
    Format:    LUKS2 → f2fs inside
    PARTLABEL: "sbnb-data"
    Mount:     /mnt/sbnb-data (read-write, noatime)
    Contents:  Device state, MQTT config, app data (or bind-mount targets)
    Lifecycle: Created and encrypted on first boot; persists across reboots/updates
```

### Internal Drive(s) (optional, mounted directly at /mnt/sbnb-data)

```
Internal drives (nvme0n1, sda, etc.)
└── Entire device(s) wiped and reformatted:
    LUKS2 → LVM PV → VG "sbnb" → LV "data" → ext4
    Mount: /mnt/sbnb-data (read-write, noatime, commit=60)

    Multiple drives are combined into a single LVM volume group
    (linear concatenation, not striped — handles mixed sizes).
    Replaces USB partition 4 when available.
```

## Encryption

All persistent data is encrypted at rest.

### Scheme

- **Algorithm**: LUKS2 with AES-XTS (kernel default)
- **Passphrase**: 44-character base64 string (256 bits entropy)
- **Key location**: Raw bytes at start of USB partition 2
- **Shared key**: Extra data drives use the same passphrase as the USB data partition

### Key Lifecycle

| Event | Action |
|-------|--------|
| First boot | Generate random passphrase → write to partition 2 → LUKS format partition 3 |
| Every boot | Read 44 bytes from partition 2 → unlock LUKS container |
| Storage config | Read same passphrase → LUKS format internal drive(s) |
| Key backup | `dd if=/dev/sdb2 bs=44 count=1 2>/dev/null` |
| Future | Migrate key from partition 2 → TPM (`tpm2_tools` already in image) |

### Key Backup and Restore

**Reading the passphrase (for backup):**

```bash
dd if=/dev/sdb2 bs=44 count=1 2>/dev/null
```

This outputs a printable string like `K7xR2pQ...=` that can be saved as text.

**Restoring from passphrase (if key partition is damaged):**

```bash
# Write passphrase back to key partition
echo -n "YOUR_BACKED_UP_PASSPHRASE" | dd of=/dev/sdb2 conv=notrunc

# Or open LUKS manually with the passphrase as a file
echo -n "YOUR_BACKED_UP_PASSPHRASE" > /tmp/key
cryptsetup luksOpen /dev/sdb3 sbnb-data --key-file /tmp/key --keyfile-size 44
shred -u /tmp/key
```

### Obfuscation

Partition 2 is typed as "Microsoft Reserved" so all operating systems ignore it. The entire partition is filled with random data, making the 44-byte passphrase boundary invisible without knowing the exact offset and length.

## Boot Flow

### First Boot

```
power on
  → EFI boots from USB partition 1
  → sbnb-storage.service runs boot-sbnb-storage.sh
  → mount /dev/sdb1 → /mnt/sbnb (read-only)
  → detect: partition 3 doesn't exist
  → fix GPT backup header (image was 512 MB, dongle is larger)
  → create partition 2 (1 MiB, key) + partition 3 (rest, data)
  → fill partition 2 with urandom, write passphrase at offset 0
  → LUKS2 format partition 3 with passphrase
  → mkfs.f2fs inside LUKS container
  → mount → /mnt/sbnb-data
  → start docker, sbnb-mqtt reconciler (parallel)
  → sbnb-init.service: credentials + banner (parallel)
  → device registers via MQTT bootstrap
```

### Small Disks

If the disk is smaller than 600 MiB (e.g., the raw QEMU image without resizing),
partition creation is silently skipped. Services use optional references (e.g.,
systemd's `EnvironmentFile=-` prefix) and handle missing mounts gracefully.

### Normal Boot

```
power on
  → EFI boots from USB partition 1
  → sbnb-storage.service runs boot-sbnb-storage.sh
  → mount /dev/sdb1 → /mnt/sbnb (read-only)
  → detect: partition 3 exists and is LUKS
  → read passphrase from partition 2
  → cryptsetup luksOpen → mount → /mnt/sbnb-data
  → mount internal drive at /mnt/sbnb-data (if available)
  → start docker, sbnb-mqtt reconciler (parallel)
  → sbnb-init.service: credentials + banner (parallel)
  → reconciler connects, receives desired state
```

### Storage Configuration (internal drives)

Triggered by the reconciler when desired state includes `storage.devices`:

```
reconciler receives desired state with storage config
  → for each device in storage.devices:
      → wipe device (remove DM layers, wipefs, zero first 4 MB)
      → LUKS2 format with passphrase from USB partition 2
      → cryptsetup luksOpen
      → pvcreate (LVM physical volume)
  → vgcreate "sbnb" from all PVs
  → lvcreate -l 100%FREE (linear, full capacity)
  → mkfs.ext4
  → migrate state from overlay → mount at /mnt/sbnb-data
  → restart Docker for new storage
```

## Directory Structure

```
/mnt/sbnb/                              ← USB partition 1 (read-only)
├── EFI/Boot/bootx64.efi               ← EFI boot image
└── mqtt/
    ├── mqtt.conf                       ← MQTT broker config (bootstrap)
    ├── ca.crt                          ← CA certificate
    ├── bootstrap.crt                   ← Bootstrap client cert
    └── bootstrap.key                   ← Bootstrap client key

/mnt/sbnb-data/                         ← USB partition 3 (encrypted)
├── state/
│   ├── mqtt.conf                       ← MQTT runtime config (post-adoption)
│   ├── device-uuid                     ← Device UUID
│   ├── device.crt                      ← Device certificate (from provisioning)
│   ├── device.key                      ← Device private key
│   ├── desired-state.json              ← Last applied desired state
│   └── docker-compose.json             ← Generated compose file
├── apps/                               ← App instance volumes
│   └── {instance-name}/{volume}/       ← Per-app persistent data
├── docker/                             ← Docker data root
│   ├── overlay2/                       ← Image layers, container filesystems
│   ├── containers/                     ← Container metadata
│   └── volumes/                        ← Docker managed volumes
└── cache/                             ← Temporary files
```

When an internal drive is configured, `/mnt/sbnb-data` is the internal drive (mounted directly by `boot-sbnb-storage.sh` or the reconciler). Everything — state, apps, docker — lives on the fast internal drive.

## Filesystem Choices

| Mount | Filesystem | Rationale |
|-------|-----------|-----------|
| `/mnt/sbnb` | VFAT | EFI standard; must be FAT for UEFI boot |
| `/mnt/sbnb-data` (USB) | f2fs | Flash-friendly, log-structured; converts random writes to sequential; `noatime` |
| `/mnt/sbnb-data` (internal) | ext4 | Standard for internal drives; `noatime,commit=60` for batched journal writes |

## Security Properties

| Property | Status |
|----------|--------|
| Data at rest | Encrypted (LUKS2/AES-XTS) on all partitions |
| Key storage | USB partition 2 (obfuscated as Microsoft Reserved) |
| Key strength | 256-bit random (44 chars base64) |
| Drive reuse | Full wipe before formatting (DM teardown + wipefs + zero 4 MB) |
| Drive removal | Data unreadable without USB dongle (key on USB) |
| USB removal | State (certs, UUID) lost — device re-bootstraps from scratch |
| TPM upgrade path | `tpm2_tools` in image; future: seal key to TPM hardware |

## Future: TPM Key Storage

The current architecture stores the LUKS key on a partition (sdb2). The key can
be migrated to a TPM for hardware-backed security:

1. `BR2_PACKAGE_TPM2_TOOLS=y` is already in the image
2. Future flow: read key from TPM instead of sdb2
3. The LUKS container itself doesn't change — only the key source

```bash
# Seal passphrase into TPM
tpm2_createprimary -C o -c primary.ctx
tpm2_create -C primary.ctx -u key.pub -r key.priv -i /dev/sdb2
# ... then update boot script to unseal from TPM instead of reading sdb2
```

## Implementation Files

| File | Purpose |
|------|---------|
| `board/sbnb/sbnb/rootfs-overlay/usr/bin/boot-sbnb-storage.sh` | Partition creation, LUKS setup, mount internal storage |
| `board/sbnb/sbnb/rootfs-overlay/usr/bin/boot-sbnb-init.sh` | Device credentials, banner |
| `board/sbnb/sbnb/rootfs-overlay/usr/bin/sbnb-efi` | Unified EFI boot entry management (fix/update/confirm/status) |
| `board/sbnb/sbnb/rootfs-overlay/usr/bin/sbnb-mqtt-reconciler` | Internal storage setup (LUKS+LVM), app lifecycle |
| `board/sbnb/sbnb/rootfs-overlay/usr/lib/systemd/system/sbnb-storage.service` | Runs boot-sbnb-storage.sh at startup |
| `board/sbnb/sbnb/rootfs-overlay/usr/lib/systemd/system/sbnb-init.service` | Runs boot-sbnb-init.sh (parallel, non-critical) |
| `configs/sbnb_defconfig` | Buildroot packages (cryptsetup, e2fsprogs, lvm2) |
| `board/sbnb/sbnb/kernel-config` | Kernel modules (dm_crypt, crypto) |
