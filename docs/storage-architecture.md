# Storage architecture

## Overview

Reefy separates boot media from persistent device and application data:

| Mount point | Backing storage | Purpose |
|---|---|---|
| `/mnt/reefy` | Currently active EFI slot, read-only | Boot image and bootstrap or device-specific files. |
| `/mnt/reefy-data` | Reefy LVM data LV on internal disks or USB fallback | Docker data, apps, desired state, credentials, and backups. |
| `/mnt/reefy-data/state` | Optional thick state LV | Control-plane identity and state isolated from thin-pool exhaustion. |
| `/mnt/reefy-data/apps/<instance>/<volume>` | Directory or per-volume thin LV | App data with ownership, backup selection, and a bulk/state policy on class-aware firmware. |

Fresh storage is provisioned during adoption, not by the boot oneshot. At
later boots, the storage service only discovers, opens, activates, and mounts
the existing stack.

## Boot-device partition layout

```text
Reefy boot disk
|-- partition 1, 1 GiB, FAT, EFI System Partition, label reefy-a
|-- partition 2, 1 GiB, FAT, EFI System Partition, label reefy-b
|-- partition 3, 1 MiB, Microsoft Reserved type, raw LUKS key material
`-- partition 4, remaining space, optional USB persistent-data fallback
```

Partitions 1 and 2 exist in the flashed image. Adoption creates partition 3
when it is absent. If no internal disk is selected, adoption also creates
partition 4 from 2050 MiB to the end of the boot disk.

The helper that constructs partition device names handles both conventional
names such as `/dev/sda4` and digit-ending devices such as
`/dev/nvme0n1p4`.

## Persistent storage stack

Current installations use the same stack on internal disks and the USB
fallback:

```text
physical device or devices
  -> LUKS2 container per device
  -> LVM physical volume per opened mapper
  -> volume group reefy
       |-- thick LV reefy_state, XFS
       |-- thin pool reefy_pool, 512 KiB chunks
       |    |-- thin LV reefy_default, XFS
       |    `-- thin LV per capped or backup-enabled app volume, XFS
       `-- legacy flat LV data, ext4, mounted when present
```

Multiple selected internal disks become physical volumes in the same LVM
volume group. Reefy does not add mirroring or parity, so this is capacity
aggregation rather than a redundant storage design.

### Default data LV

`reefy_default` has a virtual size equal to the thin pool and is mounted at
`/mnt/reefy-data`. It contains Docker storage, ordinary app-volume
directories, caches, and the state directory when the separate state LV is
not available.

Fresh default LVs use XFS because dynamic inode allocation avoids the large
up-front inode-table cost seen with full-pool-size ext4 volumes. Existing ext4
or f2fs filesystems are detected and mounted without reformatting.

### Thick state LV

Fresh volume groups reserve `reefy_state` before the thin pool consumes the
remaining space. Its size is:

```text
min(4 GiB, 10 percent of VG size), with a 256 MiB floor
```

It is a thick XFS LV outside the thin pool and mounts at
`/mnt/reefy-data/state`. This keeps device identity, MQTT configuration,
desired state, LAN certificates, and control-plane files writable even if app
or Docker activity fills the thin pool.

Existing devices whose pool already owns all free extents cannot add this LV
in place. They continue storing state on `reefy_default` until reprovisioned
or manually migrated.

### Per-app thin LVs

An app volume receives its own thin LV when it is backup-enabled or declares a
capacity percentage. Reefy derives a stable LV name from the absolute host
path, creates XFS on first use, and mounts it before Docker.

Backup volumes need their own LV so Reefy can take a consistent LVM snapshot.
Capped volumes on legacy policies use the thin LV's virtual size: when a
manifest sets `cap_pct`, only that percentage of the pool is addressable by the volume.
An uncapped backup LV uses the full pool virtual size but consumes physical
chunks only as data is written.

Reefy does not mount a new LV over a non-empty legacy directory. It preserves
the existing files and leaves that volume on the default LV rather than
hiding data.

## Storage pressure quotas

Class-aware firmware retains thin provisioning and XFS. Each app-instance volume
has an XFS project quota, whether it is a directory on the default LV or has its
own LV. Changing its quota changes the free-space allowance visible at that
volume without resizing XFS or changing Frigate's code.

```text
One physical thin pool
    |
    +-- storage guard: one shared allocation ledger
         +-- bulk: recordings, disposable caches
         +-- runtime: Docker images, writable layers, bounded logs
         +-- state: app databases and configuration

Separate thick state LV: Reefy control identity and recovery state
```

App authors declare `storage_class: "bulk"` or `storage_class: "state"` per
volume. Omission defaults to `state`. The percentages and absolute headroom
values below are Reefy policy, not app-manifest settings. A class controls
growth priority; it is not a partition, a guaranteed reservation, a database
size cap, or permission for Reefy to delete app files.

The guard samples exact device-mapper data and metadata counters and XFS quota
usage every ten seconds. It revokes and verifies old allowances before granting
that capacity elsewhere. Multiple apps cannot each claim all remaining space.
The loop does not walk directory trees, run `du`, or perform image deletion.

### Capacity and priorities

For physical thin-pool data capacity `P`, excluding metadata and the thick state
LV, the initial headroom rules are:

```text
state headroom   = min(10% of P, 100 GB)
runtime headroom = min(10% of P, 100 GB)
base emergency  = min(max(5% of P, 4 GB), 50 GB)
emergency       = max(base emergency, physical rate * response bound + in-flight work)

state ceiling   = P - emergency
runtime ceiling = state ceiling - state headroom
bulk ceiling    = runtime ceiling - runtime headroom
```

GB is decimal. Pool capacity is smaller than a disk's advertised capacity.
These examples assume the measured response requirement fits within the base
emergency reserve:

| Usable pool | Bulk ceiling | Runtime ceiling | State ceiling | Emergency |
|---|---:|---:|---:|---:|
| 32 GB | 21.6 GB | 24.8 GB | 28 GB | 4 GB |
| 128 GB | 96 GB | 108.8 GB | 121.6 GB | 6.4 GB |
| 512 GB | 384 GB | 435.2 GB | 486.4 GB | 25.6 GB |
| 1 TB | 750 GB | 850 GB | 950 GB | 50 GB |
| 10 TB | 9,750 GB | 9,850 GB | 9,950 GB | 50 GB |

All existing allocations count toward each boundary. A large database therefore
reduces available media capacity; the extra 100 GB state band is not a maximum
database size. A sudden database burst cannot reclaim occupied media instantly.
When actual headroom runs out, its allocations can fail too. A larger physical
rate or response requirement increases the emergency reserve beyond the base
cap, and an impossible budget blocks new allocation.

The controller closes lower-priority grants within a 64 MiB transition window
before opening the next band. This prevents small unusable quota fragments from
stranding space intended for state. The physical watchdog uses the controller's
response margin as well, so snapshot growth can trigger containment before
that margin is consumed. Rate observations share one 64 MiB burst allowance,
replenished with elapsed time. Short admission-triggered samples do not turn a
permitted burst into an assumed sustained rate; repeated growth beyond the
rate-plus-burst envelope increases the persisted safety bound.

### Application and Docker behavior

Frigate's own low-space maintenance can see a reduced media allowance and delete
recordings. Keep its config/database on `state` and recording scratch on `bulk`.
Other applications receive allocation errors at their limits. Reefy does not
delete generic bulk data, clips, exports, or databases to manufacture capacity.

Docker shares the pool with app volumes. Its images, writable layers, and logs
are separate consumers from bind-mounted app data. Writable layers use native
Docker/XFS quota domains; the guard preserves Docker's project ownership and
accounts for their aggregate allowance. New writable layers start with a
16 MiB allowance; managed creation reserves that allowance before Docker runs,
and the guard may grow each layer up to 2 GiB. Existing larger layers keep their
data, but do not receive additional growth until they are below that limit.
Logs use `json-file`, at most three
20 MiB files per container. Existing managed containers acquire explicit logging
settings when Compose recreates them; restarting alone does not retrofit logs.

Image maintenance runs separately from the fast guard. It retains images until
three calendar months continuously unreferenced, with UTC end-of-month clamping.
Running and stopped containers, desired images, pending work, and selected
rollback references protect an image. Missing history starts a new observation
period; unreliable time postpones deletion. An event-observation gap, including
a maintenance-worker restart, conservatively starts a fresh observation period
rather than assuming the image stayed unused. References and eligibility age are rechecked before
Docker removes an eligible image. Images are not evicted early under pressure;
retained images instead reduce bulk headroom or delay a new pull.

### Projects, upgrade, and restore

A project ID is an internal XFS inode label, scoped to a filesystem. Each
independent app-instance volume has its own ID; services sharing a volume share
that ID. The number does not encode a storage class and is not a fleet-wide
identifier. Reefy records ownership durably and avoids Docker's allocation
range. Existing files need explicit tags, and new descendants inherit their
directory's project. Migration verifies tags and directory inheritance without
following symlinks or crossing other mounts. Private, non-recursive filesystem
views also cover old files hidden beneath mountpoints. They do not traverse or
retag the filesystem mounted above those files.

Firmware advertises `storage_pressure_quotas: 1`, and desired state activates the
policy with `storage_pressure_policy: {"version": 1}` plus effective per-volume
classes. A device can boot its cached legacy policy while waiting for the new
configuration. On first activation, Reefy holds affected writers, mounts XFS with
project accounting, tags and verifies existing data, then releases startup.
No operator pre-tagging is needed. Interrupted work resumes using the saved IDs;
later boots verify protection before starting Docker and skip completed tree walks.
Missing or invalid enforcement keeps writers held while control and SSH remain
available. Existing ext4/f2fs layouts are rejected before a new class policy
replaces their cached legacy configuration; they require a separate filesystem
migration. Once protection is active, a missing or changed filesystem fails
closed rather than falling back to an unrestricted directory.

Backup snapshots retain source inode metadata. A restored instance receives its
own destination volume assignments; restored files must match those assignments
before the app starts. The source app may remain installed and running. Numeric
ID equality across different filesystems does not imply shared quota ownership.
Imported mismatches can be repaired only while the destination is unused;
existing destination quotas remain enforced throughout. A completed-restore
marker does not bypass the ownership check. Boot recovery releases operation
reservations from proven previous boots, while reservations with uncertain or
same-boot ownership remain conservative.

Legacy `cap_pct` continues to limit an LV's virtual size. It is not reinterpreted
as a physical threshold. The catalog retains the last classless manifest for
older firmware, and class-aware releases require compatible firmware. Existing
LVs are not shrunk or reformatted during quota activation. Once active, an
accidentally omitted activation marker does not remove protection.

### Limits of the mechanism

Project-quota bytes differ from physical thin-pool usage. Snapshot COW, thin-chunk
granularity, filesystem metadata, and delayed discard can allocate or retain
physical blocks without increasing an app's quota usage. Deleted bytes are not
credited until the pool reports real reclamation. Quotas are operational pressure
controls, not exact instantaneous physical ceilings or an uninterrupted-backup
guarantee. An independent watchdog holds unsafe writers when measurements are
stale or physical/metadata pressure cannot be contained. Hardware qualification
must establish an adequate response reserve before rollout.

The policy protects against accidental unbounded growth. It is not a security
boundary against privileged or deliberately hostile apps that alter project IDs
or access host block devices. Mixed ownership from copying or restore is checked
at lifecycle boundaries; the fast guard does not recursively rescan every file.

## Encryption and key handling

Every newly provisioned persistent data device is formatted as LUKS2. A single
fresh 44-character base64 key is written at the start of partition 3 and used
for all data devices provisioned in that operation.

The key partition is raw, has no filesystem, and is marked with the Microsoft
Reserved GPT type so desktop operating systems normally ignore it. The full
1 MiB partition is filled with random data before the key bytes are written at
the known offset.

LUKS is opened with discard pass-through and crypto-CPU submission enabled.
These flags are persisted in the LUKS header on provisioning. Filesystems also
mount with discard so deleted thin-pool chunks can reach the physical device.

This design protects an internal data disk removed without the Reefy boot
disk. It does not protect against an attacker who obtains both the boot disk
and the encrypted data disks, because the unlock key is on the boot disk. A
future TPM-sealed key can strengthen that boundary without changing the LUKS
or LVM layout.

## Adoption flow

Before adoption, `/mnt/reefy-data` may be only a writable rootfs-overlay
directory. Adoption calls the shared storage implementation:

1. Find the disk containing `reefy-a` or `reefy-b`.
2. Create partition 3 when missing.
3. Look for existing Reefy-encrypted internal disks that the current key can
   open, supporting a restore or reattachment case.
4. If the desired storage list names internal disks, tear down existing
   device-mapper layers, wipe signatures and the first 4 MiB, and provision
   those whole disks.
5. Otherwise create partition 4 and provision it as the USB fallback.
6. Write one fresh key, create and open the LUKS containers, and create LVM
   physical volumes.
7. Create or extend VG `reefy`.
8. Create `reefy_state`, then `reefy_pool`, then `reefy_default` when this is a
   fresh layout.
9. Copy bootstrap state aside, mount the persistent LV, restore state, mount
   `reefy_state`, and create standard directories.

The new storage becomes active during the adoption apply. No reboot is
required.

Provisioning selected disks is destructive by design. The dashboard and
desired state identify the devices to use; the data plane wipes them before
creating Reefy's encrypted stack.

## Boot flow

`boot-reefy-storage.sh` follows an internal-first, USB-fallback policy:

1. Mount the active A/B ESP read-only at `/mnt/reefy`.
2. Resolve partition 3 as the key file.
3. Scan every non-boot block device for LUKS and try Reefy's key.
4. Activate VG `reefy` and mount `reefy_default`, or the legacy `data` LV,
   when found.
5. Mount `reefy_state` at the nested state path when it exists.
6. If no internal stack mounted, open USB partition 4 and mount the same LVM
   layout or a supported legacy direct filesystem.
7. If neither path is persistent, create bootstrap state directories in the
   writable rootfs overlay.

The script detects the actual LV filesystem before choosing mount options.
XFS gets `noatime,discard`; ext4 keeps
`noatime,commit=60,discard`; legacy f2fs uses `noatime`.

## App-volume boot ordering

`reefy-app-volumes.service` runs after base storage and before Docker. It reads
the persisted desired state, collects backup paths and capped-volume paths,
and mounts each thin LV through a shared file lock.

This closes a boot race where Docker could restore a container before its
bind-mount target was mounted. The operation is idempotent, and the running
reconciler uses the same locked primitive for app install or removal.

When an app instance is deleted, the data plane waits until Compose has
removed its container, unmounts its per-volume LVs, and removes them. Merely
disabling backup does not delete the volume because the instance still appears
elsewhere in desired state.

## Directory structure

```text
/mnt/reefy/
|-- EFI/Boot/bootx64.efi
|-- mqtt/
|   |-- mqtt.conf
|   |-- ca.crt
|   |-- bootstrap.crt
|   `-- bootstrap.key
`-- reefy/                         device-specific files preserved by OTA

/mnt/reefy-data/
|-- state/
|   |-- mqtt.conf
|   |-- device-uuid
|   |-- device.crt
|   |-- device.key
|   |-- desired-state.json
|   |-- docker-compose.json
|   |-- backup/
|   |-- lan/
|   `-- llm-proxy/
|-- apps/
|   `-- <instance-uuid>/
|       `-- <volume-name>/
|-- docker/
`-- cache/
```

Docker's configured data root is `/mnt/reefy-data/docker`.

## Compatibility behavior

The implementation retains read and mount compatibility for earlier layouts:

- `sbnb-a` and `sbnb-b` labels can still identify a legacy boot disk.
- A flat VG `reefy` LV named `data` is mounted when `reefy_default` is absent.
- Existing ext4 app LVs are mounted and never reformatted.
- A legacy direct f2fs or ext4 filesystem inside USB LUKS partition 4 remains
  mountable.
- Existing non-empty app directories are not shadowed by new thin LVs.

Compatibility is intentionally conservative. Devices keep working, but some
new capabilities such as snapshot-backed per-volume backup require a fresh
thin-pool layout.

## Implementation files

| File | Responsibility |
|---|---|
| `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/storage.py` | Provisioning, LUKS/LVM lifecycle, mounts, app volumes, and reclaim. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/reefy/reefy/shared.py` | Shared layout constants and partition-name helper. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/boot-reefy-storage.sh` | Boot discovery and mount flow. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/reefy-mount-volumes` | Pre-Docker per-app mount entry point. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/systemd/system/reefy-storage.service` | Base storage boot barrier. |
| `board/reefy/reefy/rootfs-overlay/usr/lib/systemd/system/reefy-app-volumes.service` | Per-app volume boot barrier. |
| `board/reefy/reefy/rootfs-overlay/etc/docker/daemon.json` | Docker data-root configuration. |
| `docs/storage-chunk-size-study.md` | Measurements behind the 512 KiB thin-pool chunk and XFS choices. |
