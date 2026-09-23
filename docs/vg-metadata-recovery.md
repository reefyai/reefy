# Outer VG metadata recovery

Boot now distinguishes an unlocked internal data disk with unreadable VG
metadata from a fresh device. LVM diagnostics are retained. If guarded
recovery fails, the storage unit fails and its control/reconciler dependents
do not start with an ephemeral bootstrap identity. SSH remains available on
dev-shell firmware for diagnosis. Existing thin-pool and XFS repair paths
remain responsible for failures after normal VG discovery.

The automatic path is limited to one PV with valid LVM label/PV/MDA header
checksums, one metadata area, and a committed VG text checksum failure.
It will not roll back readable committed metadata. It searches complete
surviving records, including ring wrap, and requires agreement on physical
geometry and persistent LV definitions. Current thin metadata must pass
upstream `thin_check`; pool geometry, thin inventory and persistent creation
transactions must agree. Unknown or missing thin IDs are refused.

Snapshot identities are quarantined: a new LV identity, no old origin/tags,
read-only access and activation skipped. The pool transaction in the new VG
record is reconciled to the validated current thin metadata; no thin-pool
transaction or mapping is rewritten. Unsupported segments, changing
persistent definitions and unexplained transaction gaps are refused.

Before any VG write, read-only thin devices expose XFS superblocks for
geometry checks. Filesystems are not mounted and their logs are not replayed.
The kernel's thin-pool `read_only` feature prevents metadata commits;
read-only thin devices prevent data writes. XFS sizes exceeding the surviving
LV definition and duplicate persistent filesystem identities are refused.

Original PV-prefix bytes, thin metadata, candidate VG text and a hash report
are archived on the boot ESP under `recovery/`. The archive is capped at
64 MiB and requires additional free space. A durable attempt marker is
written before `pvck --repairtype metadata`. Existing evidence or an attempt
marker prevents another historical reconstruction until an operator reviews
it. Evidence is never automatically rotated away. Both PV prefix and thin
metadata are reread before repair to detect intervening changes.

## Limits requiring review

Surviving history and current thin metadata cannot establish that every
unrecorded rename, tag/ownership change, or configuration operation is absent.
The XFS checks constrain unsafe size rollback but do not validate all file
contents or prove the cause of corruption. This is bounded best-effort
reconstruction, not a guarantee of zero data loss. The evidence archive and
quarantined snapshots must be retained for review. Multi-PV, non-XFS, large
metadata, conflicting histories, or low ESP space require operator recovery.

## Verification

The service repository's `VG recovery E2E` workflow uses actual firmware on
synthetic QEMU boot and NVMe disks. Its firmware mode invokes no recovery
helper from the harness: corruption is followed by a full reboot, then
identity/application/snapshot assertions and a second normal reboot. Refusal
scenarios assert unchanged PV metadata and a stopped control plane. The
older explicit-reconstruction lab remains available for comparisons but is
not the firmware recovery acceptance test.
