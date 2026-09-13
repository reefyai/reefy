# Bulk storage protection

Reefy bounds volumes declared `bulk` using XFS project quotas. `state` is the
compatible default and is not assigned a new quota in this implementation.
Frigate media is bulk; its config database remains outside the media quota.
Docker storage, images, container writable layers and backup admission are not
redesigned by this feature. It mitigates bulk growth, not every source of physical
pool exhaustion.

The independent `reefy-storage-guard` service reads cached desired state and
samples actual thin-pool data and metadata counters every ten seconds. It does
not query cloud services or Docker. Installed kernel quotas remain when it exits.

## Internal sizing

All quantities use bytes; GB means 1,000,000,000 bytes. For physical thin-data
capacity P, with current bulk usage U and physical free space F:

- Withheld headroom H = min(max(20% of P, 4 GB), 200 GB).
- Device-wide minimum total bulk quota = min(1% of P, 1 GB).
- Total target = max(minimum total bulk quota, U + F - H).

Round limits conservatively to XFS blocks, never zero (which means unlimited).
The headroom limits do not cap total bulk storage. Other writers' growth lowers
the bulk target, possibly below its current usage. At 90% thin metadata usage,
reduce the target to the bulk floor. This does not stop other writers or repair
an unhealthy pool. Pools too small for the headroom report degraded protection.

Bulk directories on one filesystem share one project, so they use its remaining
allowance first come, first served. Separate filesystems cannot share a kernel
quota: divide spare growth equally and reductions by usage, under one total
budget. Reductions are verified before increases. There can be adjustment delays
between filesystems. No full per-device budget is duplicated per app.

## Setup and repair

Normal XFS mounts enable `pquota,discard`. A successful remount is not proof that
accounting was enabled. The first accounting mount can scan existing inodes;
there is no additional quota tagging barrier before app startup.

A background process tags existing bulk inodes, enables directory inheritance,
and verifies the result. It skips following symlinks, rejects nested mounts and
cross-boundary hardlinks, and retries incomplete scans. Ownership is rechecked
on service restart, root replacement, policy changes and six-hour audits. A
single worker cannot block the ten-second pressure loop. Failed quota setup
retains existing limits and keeps correctly mounted apps running. Resync never
stops Docker. Genuine missing app volumes remain subject to existing mount checks.

Ownership records live under `/mnt/reefy-data/state/bulk-storage`; local status
is `/run/reefy/bulk-storage/status.json`. Stage changes and physical pressure
are logged by `reefy-storage-guard` and included in the existing log publisher.
Status is explicitly bulk-only; active does not promise full-pool containment.
No new remote status API or automatic external notification is introduced.

Project IDs use a separate high range and are persisted by filesystem UUID.
Directory inheritance handles normal new files. A restored/replaced volume is
reconciled, not assumed safe because it carries a familiar numeric project ID.
State transitions remove bulk ownership in the same background workflow.

## Limits and application behavior

Frigate sees quota-limited free space at its recordings path and can invoke its
normal low-space cleanup without a code change. Large downward adjustments can
require several five-minute cleanup passes. If cleanup cannot reclaim enough,
new media allocations fail locally; config writes use their separate storage.
Recording cleanup does not remove every media type, such as exports.

A project quota is not a physical reservation. Deletion may return quota credit
before discard returns thin chunks. Snapshots, filesystem overhead and partial
chunks also decouple physical usage from file usage. The controller uses real
pool counters and preserves headroom, but cannot guarantee containment of every
burst or unrelated writer. Quota-only failure reports degraded coverage.

The standard weekly `fstrim.timer` also reclaims accumulated free extents. Reefy
overrides `fstrim.service` to use `--all`, including dynamically mounted app
filesystems absent from `/etc/fstab`. The upstream `--listed-in` fallback stops
at a nonempty fstab and otherwise misses those volumes. This complements online
`discard`; it does not run trim in the ten-second guard loop. Trim output reports
ranges submitted for discard, not actual bytes recovered; only thin-pool counters
determine new quota allowances.
