# Best-effort background VG metadata redundancy maintenance

Normal LVM autoactivation and filesystem mounting are unchanged. After storage
setup, a separate oneshot service is queued asynchronously. Storage, application
startup and boot do not require maintenance to succeed. The service remains
completed until reboot and has no automatic restart or timer.

A read-only bounded check detects damaged current metadata text on supported
single-PV, two-area Reefy storage. Healthy and legacy one-area PVs request no
metadata writes. Both invalid copies, conflicting current records, invalid
headers, changed geometry and unsupported layouts are deferred for review.

Only confirmed text damage with a validated surviving current copy triggers
stock `vgck --updatemetadata reefy`. No exported snapshot is replayed, no vendor
source is patched, and the helper never unmounts, deactivates, suspends volumes
or changes LVM system configuration. LVM locking remains enabled. Per-command
`wait_for_locks=0` skips busy locks and `prioritise_write_locks=0` avoids giving
this background process writer preference. An acquired metadata lock can still
briefly delay competing LVM management operations; ordinary filesystem I/O is
not serialized by that lock. No hard runtime guarantee is claimed on slow disks.

At most three attempts total use one- and two-second backoff. Each native repair
is independently verified; successful exit alone is insufficient. Busy discovery
is bounded, unsupported metadata and failed verification defer, and native
operational failures stop further writes. No generic deadline interrupts a
metadata write. The service has no boot-critical dependency; an uninterruptible
storage command can nevertheless extend shutdown, rather than be killed mid-write.

Results, bounded native output and metadata hashes are recorded in
`/run/reefy/pv-redundancy.json` and mounted persistent state under
`state/pv-redundancy/last-result.json`. Exhausted attempts leave redundancy
degraded and diagnostics available without setting the storage failure marker.

E2E injects damage on either copy, requires actual firmware recovery, verifies
data/identity/key preservation, and checks a healthy reboot changes no metadata.
Real lock contention and injected zero-exit/no-write commands exercise the
background failure paths while mounted storage stays usable. Unit tests also
cover supported-source validation, false-success retry, native errors and bounds.
