# Build and VM I/O separation plan

Status: proposed only. This change executes no storage migration, formatting,
live LVM/filesystem tuning, deployment or full E2E. Device certificate/topic
isolation, six software workers, hardware gates and all deadlines remain intact.

## Findings and cache correction

Firmware builders use separate writable Buildroot caches, with a read-only seed
from the primary. Identical source revisions alone do not establish identical
cache inputs. Read-only comparison found identical tracked build inputs and Rust
compiler identity/bytes, but different checkout locations in BR2_DEFCONFIG and
BR2_EXTERNAL_REEFY_PATH. The old package key hashed these absolute locations.
The corrected key normalizes only checkout-owned locations, preserving external
paths and all effective configuration assignments, recipes and output hashes.

Kernel keys also included archive packaging and test code. Such changes do not
compile the kernel. The corrected selector excludes known archive-only tools and
test files; unknown build tools, configuration, patches, workflow and compiler
inputs remain covered. Reconstructing compile inputs across the prior runtime
and RAM-staging revisions produces equal input digests with this selector.

Old markers lack per-file provenance, so the complete historical kernel-key
transition cannot be reconstructed from their hash alone. New kernel component
manifests retain relative input names and hashes. Package records retain hashed
configuration components. Changed input names are logged, without plaintext
configuration values or deployment credentials. Successful markers still require
successful build/archive verification; interrupted builds cannot bless a cache.
The package-key format transition intentionally incurs a conservative initial
refresh. No cache is manually marked valid.

## Remaining state-LV timeout

The retained Wi-Fi adoption guest timed out after 15 seconds in `lvcreate`.
Host full I/O pressure reached about 75 percent during the provisioning window,
while physical free space stayed near 98 GiB. Subsequent read-only LVM inventory
showed the state LV had committed. This is consistent with the previously
measured host allocation stalls; it does not establish the exact kernel wait
site for this invocation. The latest fresh firmware builds were verified kernel
and package cache hits, but still compiled provider modules and regenerated
filesystem images. Cache reuse therefore did not remove competing disk writes.

Keep the current state-LV safety behavior: retry only when a valid, bounded LVM
inventory proves the LV absent. If metadata committed or its state is unknown,
stop without retrying or formatting. Do not increase the 15-second command
budget or automatically format an existing LV to hide storage contention.
Focused state-creation and formatting tests retain these checks and preserve
existing volumes.

A read-only topology check also confirms that both builder caches and VM
storage resolve through the same ext4 device to both physical NVMe controllers.
No independent build resource has been selected or provisioned.

## Target layout and capacity gate

Both physical SSDs currently back the same encrypted thin pool. The VG has no
unallocated extents. A new directory, LV, partition within that pool, or tmpfs
for more durable data would not provide the required physical I/O separation.

Use an independent physical build device or a separate physical builder host.
Leave the current VM/app/backup storage and live pool unchanged. A separate host
is also acceptable when no independent device is available. Resource selection
must establish distinct physical backing before provisioning anything.

The read-only allocation audit measured about 222.4 GiB and 133.8 GiB for the two
build caches. These are filesystem allocated-byte estimates, not unique thin-pool
extent counts; cross-filesystem copies may lose shared extents. Plan conservatively:

- Budget at least twice the current combined cache allocation for active outputs,
  refreshes and migration/rollback overlap, plus 128 GiB provisionally for checkout,
  container/build workspaces and retained artifacts.
- Keep at least 20 percent of the new filesystem free. The resulting initial
  planning floor is 1.25 TiB usable. Measure workspace/container/artifact allocation
  and peak refresh usage before selecting the resource; increase the floor if needed.
- Keep at least 100 GiB physical free on the existing VM pool before qualification,
  monitor the existing 64 GiB reserve and require read-write pool health.

Use synthetic proposed paths such as /srv/ci-build/primary and
/srv/ci-build/secondary. Map ALL bulk builder writes there:

| Data | Proposed destination |
| --- | --- |
| Buildroot output, downloads and ccache | Per-builder build-store directories |
| Runner checkout, actions temp and build logs | Per-builder workspace directories |
| Builder-private Docker/containerd data | Per-builder engine directories |
| Outer runner writable/image layers and engine metadata | Builder-only outer engine on build store |
| Final firmware, providers and persistent debug archives | Build-store artifact directories |
| Read-only secondary seed | Primary build-store cache, read-only mount |
| Shared version counter | One preserved, locked build-store directory |
| Debug staging/extracted symbols | Existing bounded unswappable RAM staging |
| VM boot/data disks, application data, cores and backups | Existing persistent VM/app storage |

Do not move the shared host Docker root or customer application volumes. Explicit
workspace mounts are necessary: moving only /buildroot-cache leaves checkout and
container-layer writes on the old storage. The outer runner containers also
install packages into their writable layers. Either move the builders to a
separate physical host, or recreate only the builders under a separate outer
engine whose entire data root is `/srv/ci-build/runner-engine`. Preserve the
customer/shared host engine and its data root. Do not try to relocate live
individual overlay directories or symlink them into a running shared daemon.
The separate engine requires its own runtime/socket/bridge names and reviewed
start/reconciliation integration; no host engine socket is exposed to jobs.

## Reviewable mount overrides and read-only checks

`tools/build-storage/build_storage.py` prepares an internal operator bundle. It
has no apply, mount, copy, format or service-control operation. For example:

```sh
python3 tools/build-storage/build_storage.py plan \
  --build-root /srv/ci-build --vm-root /srv/ci-vms > proposed-layout.json
jq '.layout.compose_overrides.primary' proposed-layout.json > primary.storage.override.json
jq '.layout.compose_overrides.secondary' proposed-layout.json > secondary.storage.override.json
```

List **every** actual VM boot, data, snapshot and scratch storage root using
repeated `--vm-root` arguments. These synthetic paths are examples, not a host
configuration. Planning creates no directories and always reports storage not
ready. Review the rendered JSON before separately authorizing resource
provisioning, copying or applying configuration.

The two overrides replace only volume entries for service `app`, by container
target. They move `/buildroot-cache`, `/_work`, `/actions-runner/_diag`, `/tmp`,
`/var/tmp`, `/var/lib/docker` and `/var/lib/containerd`. All persistent build
outputs and final artifacts must be inside those cache/work paths. The audited
runner work folder is a child of `/_work`. Private engines retain separate
stores; the secondary gets only a read-only primary seed. Both roles retain
one writable, locked version-counter directory. Explicit binds forbid automatic
host-directory creation. Existing images, environment, device mounts, privileges,
MQTT settings and network boundaries are not changed by the override.

Before any future start, run the following on the **host** in the same mount
namespace as the host container engine, after authorized provisioning/copying:

```sh
python3 tools/build-storage/build_storage.py check proposed-layout.json
```

The check requires an exact ext4/XFS build mount, at least 1.25 TiB usable and
20 percent free, and all bind source directories on that mount. It walks
partition and LUKS/LVM/device-mapper ancestry through sysfs. Shared whole disks,
shared NVMe controllers (including different namespaces), missing/cyclic ancestry,
unknown virtual backing, unmounted placeholders, nested mount escapes, read-only
filesystems and inadequate capacity all fail closed. It does not certify pool
health, copied-data integrity or performance; those remain separate gates.

These topology checks implement the same-host independent-device option. A
separate physical builder host needs equivalent reviewed physical/storage
inventory on both hosts; it cannot pass this local check by inventing remote VM
paths or mounting VM data onto the builder merely to satisfy the tool.

After the separately authorized runner cutover, capture each builder's
read-only container inspect `Mounts` and `GraphDriver` fields in a private JSON
object with `primary` and `secondary` keys (each value holds those two fields), then run:

```sh
python3 tools/build-storage/build_storage.py check-containers \
  proposed-layout.json builder-mounts.json
```

This repeats the host checks and verifies the actual bind sources, destinations
and read/write modes, including seed/counter mounts. It also requires every
outer overlay2 upper/work/lower layer to resolve inside the dedicated
`runner-engine` directory on build storage. Unknown snapshotters fail rather
than claiming their backing is verified. Added host Docker socket
mounts are rejected. This uses operator-supplied inventory and never accesses a
daemon or changes a container. Keep fresh inventory with the qualification
artifacts; do not reuse it after a restart or configuration change.

Do not apply an override to an ephemeral, reconciler-generated Compose file.
The cutover must update the builders' authoritative declarations and persistent
start mechanism, with a host mount dependency and a fresh successful storage
check before **every** start. A missing/unhealthy build mount must keep only the
builders stopped, including after host restart; creating directories on the VM
filesystem is not a fallback. Validate this restart/missing-mount behavior before
admitting work. Copy the runner diagnostics and workspace state while drained,
and preserve existing ownership and runner registration. These integration
changes belong to the separately reviewed cutover for the selected resource.

The tool is outside kernel build inputs so changing storage preparation does
not invalidate compiled firmware caches. The self-hosted Python workflow runs
its synthetic regression suite; no new live-storage CI gate is enabled yet.

Focused commands:

```sh
python3 -m unittest discover -s tools/build-storage -v
PYTHONPATH=board/reefy/reefy/tests python3 -m unittest \
  test_storage.StateLvCreateTimeoutTests test_state_format_deadline -v
```

Passing these checks reports no applied configuration and no passed full
qualification. Independent hardware and a validated cutover are still required
before claiming that build/VM contention is fixed.

## Proposed cutover procedure

1. Record source revisions, runner definitions/mounts, successful cache markers,
   allocated usage and maximum firmware version counters. Verify recoverable backups
   and a manifest of retained failed-run evidence and rollback artifacts. Do not
   delete evidence to make migration space.
2. Prepare the independent resource only as a separately authorized operation.
   Record its mount UUID, filesystem and complete lower block-device chain.
   Prove it does not resolve to the VM pool's physical backing. Do not change the
   live pool's chunk size, zeroing, discard, encryption or filesystem parameters.
3. Drain and stop only the two firmware builder runners and their private engines.
   Confirm no build/cache writers remain. Keep operator, devices and applications
   running. Do not copy a live writable cache or treat the read-only seed as a
   coherent snapshot while its primary is still building.
4. Copy cache/workspace/engine data preserving sparse files, symlinks, hard links,
   ownership, permissions and extended attributes. Compare file inventories and
   hashes of cache outputs and retained artifacts. Preserve the version-counter
   maximum and lock ownership; never permit counters to go backwards.
5. Update only builder-specific declarations, engine placement and guarded start
   dependencies as described above. Start one builder first. Inspect
   findmnt and block dependencies from both container and host views. Check that
   workspace, cache, engine, temp and artifact writes all reach the independent
   store; validate read-only seed and shared counter mounts. Leave the second
   builder stopped until this passes.
6. Run focused cache regressions and one cold, verified firmware build, followed by
   same-source warm and relocated-checkout builds. Retain all eight required
   firmware job results, source/output hashes, signing and provider checks. Distinct
   A/B build identities remain required even when compiled packages are reused.
7. Start the second builder with the same checks. Benchmark cold compilation,
   linking/module installation and image generation concurrently with six synthetic
   disk-backed guests and snapshot reads. Capture per-device throughput, latency,
   queue depth, I/O pressure, CPU/memory pressure and physical pool capacity. Use
   bounded test files or disposable guests, never raw writes to live devices.
8. Only after the cold-load benchmark and focused tests pass, request/run the normal
   full dev qualification with six workers, one shared fixture, four fresh A/B and
   rollback workflows and the protected hardware gate. No production promotion is
   part of this plan.

## Acceptance and rollback

Accept only with no storage/transport deadline failures under cold concurrent
load: snapshot identification remains inside its existing five-second budget,
Docker fault injection inside its existing 30-second transport limit, state-LV
creation inside its existing 15-second budget, and all
normal readiness/recovery/durability checks pass unchanged. Require data and
credential sentinels preserved, no OOM/reclaim-driven fallback, read-write pools,
capacity reserves, monotonic version counters and a complete hardware-qualified
record. Cache correctness must also hold for real configuration/recipe/compiler
changes, damaged outputs and interrupted builds.

If mount ancestry, integrity, capacity or cold-load checks fail, stop only new
builder jobs, drain active writers, preserve the new store for diagnosis and
restore the recorded builder-specific mount definitions. Keep the old caches and
artifacts until qualification and an explicit retirement decision. Preserve the
maximum counter from both stores under one lock before resuming; never run two
writable owners against a cache or counter. VM/app data is not migrated by this
plan and remains on its existing storage throughout rollback.

The live pool redesign and remaining zeroing/allocation investigation remain
tracked in [reefy#107](https://github.com/reefyai/reefy/issues/107). Physical build
separation reduces contention but does not itself prove the old pool's latency
has been repaired.
