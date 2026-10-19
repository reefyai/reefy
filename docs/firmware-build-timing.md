# Firmware build timing history

Record exact workflow references and step durations when changing the build
pipeline. Timings here come from GitHub Actions job/step start and completion
metadata. They are elapsed wall time, not aggregate CPU time. Main-job duration
includes compilation, debug verification and its artifact uploads; it excludes
later provider publication jobs and workflow queue time. Do not compare a failed
partial build with a successful workflow as if both completed the same work.

## System-symbol collection candidate

| Build | Candidate and cache context | Main build job | Main compilation/image step |
| --- | --- | ---: | ---: |
| [37769678650](https://github.com/reefyai/reefy/actions/runs/37769678650) | Deployed main baseline, kernel symbols already enabled | 16m01s | 8m11s |
| [37780562223](https://github.com/reefyai/reefy/actions/runs/37780562223) | Earlier package-cache candidate, cached kernel; package reuse verification failed | 13m02s | 5m39s |
| [37815213661](https://github.com/reefyai/reefy/actions/runs/37815213661) | Coredumps/system symbols, GCC default build IDs, 12 documented symbol gaps; toolchain reused from the preceding debug rebuild | 22m49s | 12m35s |

The main job increased by 6m48s (42.5%) versus the deployed baseline. The
compilation/image step alone increased by 4m24s. This is a combined-change,
unequal-cache comparison, not a controlled attribution to symbols: compiler
settings, package rebuilds, kernel-cache state and crash-capture packages differ.
The package-cache candidate does not establish a proven package speedup.

### Direct measured system-symbol work

| Work in candidate 37815213661 | Elapsed |
| --- | ---: |
| Pre-strip userspace capture, within main compilation step | approximately 3.4s |
| Verify default cross-compiler executable/shared-library identities | 8s |
| Create, verify and compress userspace debug archive | 115s |
| Upload userspace debug archive | 18s |
| Upload small lookup index | 1s |
| **Measured userspace-specific work combined** | **approximately 2m25s** |

Pre-strip duration is bounded by its invocation and the next logged make
command. Archive creation combines binary verification, source collection,
hashing and compression; these are not separately instrumented yet. Upload
speed and filesystem/cache state can vary. The compiler check took 8 seconds
with the toolchain already present; it includes much longer toolchain building
when a clean build is necessary.

Kernel debug verification/compression was 88s and upload was 44s in this
candidate. The deployed baseline already spent 90s and 44s respectively on
those steps, so kernel symbol archiving is not newly introduced overhead.

The userspace tarball is 1,180,302,697 bytes (1,125.62 MiB, approximately
1.10 GiB). Its GitHub artifact ZIP is 1,180,302,867 bytes, and the lookup index
ZIP is 357 bytes. This is additional CI artifact storage, not added runtime
firmware payload. The inventory contains 783 verified symbol originals plus
12 explicitly documented missing-DWARF binaries. Consult `SYMBOL-GAPS.txt`.

### Clean builds and remaining measurement

Changing debug mode or GCC configuration requires a clean toolchain/package
build. The preceding [37801188826](https://github.com/reefyai/reefy/actions/runs/37801188826)
attempt spent 10m32s building/checking the toolchain and 77m50s in the remaining
compilation step before failing at missing-DWARF verification. Its failed main
job lasted 89m43s. That illustrates first-build cost but is not a completed
clean-debug baseline and must not be presented as recurring collection overhead.

A successful clean build and a repeat of the exact same candidate with stable
cache/runner conditions are still needed to measure steady-state slowdown.
Provider publication completed successfully. Active workflow spans for the
validated repeat builds are recorded below; queue time is excluded. QEMU/full E2E
and hardware runtime validation are also separate from these compilation
measurements. Update this history with final end-to-end timing and a controlled
repeat before claiming a fixed percentage slowdown.

See [image/artifact size history](firmware-size-history.md) and
[crash diagnostics and trust](DEBUGGING-TRUST.md).

## Verified repeat and full validation

All builds below use firmware source `4aceef442f804ad6d5e73c2871abc9fe23a53c39`.
The kernel toolchain is prepared before configuration, including Buildroot's
selected Rust compiler. Kernel ABI equality passed across the A/B builds.

| Workflow | Main job | Compilation/images | Active workflow span | Package-cache result |
| --- | ---: | ---: | ---: | --- |
| [37842913956](https://github.com/reefyai/reefy/actions/runs/37842913956) | 18m59s | 8m45s | 25m04s | Initial revised candidate |
| [37850019454](https://github.com/reefyai/reefy/actions/runs/37850019454) | 14m46s | 5m13s | 19m13s | Both packages verified unchanged |
| [37857547242](https://github.com/reefyai/reefy/actions/runs/37857547242) | 16m10s | 6m10s | 20m49s | Conservative refresh after input-key change |

The real warm A/B build verifies cache reuse, rather than inferring it from
a shorter job. Other runs can still miss the conservative input key. These
measurements do not isolate a fixed speedup or guarantee a cache hit for every
same-commit build across runner/cache environments.

Userspace archive verification/compression took 115-116 seconds, archive upload
15-17 seconds, and lookup-index upload one second in these three builds. The
warm default-compiler check took 7-8 seconds. Archive ZIP size for the selected
candidate was 1,180,303,216 bytes, approximately 1.10 GiB. These archives are
kept outside the runtime firmware image.

Full six-worker E2E passed 39/39 scenarios in 71m36s. The protected hardware
gate passed in 22m30s, including actual accelerator workloads. The harness
reference was `4a6bb8e6c78e160d605396eefdba83dff39f2ca5`; backend reference was
`6fe125fa335a56915f57b750de8d9223585b7b0d`. Targeted thin-pool and A/B pilots
passed before the full suite and are excluded from its wall time.

The full suite was 9m10s (14.7%) longer than the preceding 38-scenario green
run at 62m26s. The scenario set now includes source-level coredump validation;
firmware publication and cache conditions also differ. The comparison alone
cannot attribute that increase to debug-symbol collection.

## Bounded kernel/userspace archive concurrency benchmark

[Benchmark run 39177262190](https://github.com/reefyai/reefy/actions/runs/39177262190)
used archive tools revision `b84162e4910a0056d7202599dd0bf55074b9668c` and one
unchanged cached firmware build. Its cache-producing build job succeeded in
run `39177237138`; obsolete queued publications from that superseded review
commit were cancelled afterward. The benchmark did not mutate the build cache
or publish firmware. Input os-release SHA256 was
`20bf58920ed49084cafd0b805bd18f989b26aac5492a14b04dce856f8d8bf5a0`.

Both subprocesses used the same 16-CPU affinity, including when run in parallel.
Each trial wrote isolated outputs. ABBA order balances a simple warm-cache/time
trend; this is two samples per mode on a shared host, not a broad performance
baseline. Kernel/userspace validation stayed enabled. The non-metadata payload
hash inventories matched in all four trials; metadata embeds each trial's
isolated source path and is excluded from that comparison.

| Trial | Mode | Kernel creation | Userspace creation | Combined wall time |
| --- | --- | ---: | ---: | ---: |
| 1 | Sequential | 91.96s | 155.54s | 247.50s |
| 2 | Parallel | 99.03s | 151.84s | 151.84s |
| 3 | Parallel | 94.76s | 147.45s | 147.45s |
| 4 | Sequential | 88.44s | 135.78s | 224.23s |
| Mean | Sequential | | | **235.86s** |
| Mean | Parallel | | | **149.65s** |

Measured archive creation saving: **86.22s (36.55%) per build**. Four fresh
builds project to approximately **5m45s** saved if this overlap is adopted and
remains on the critical path. Upload time, workflow queues, benchmark inventory
comparison and full-suite time are excluded. This is not a measured reduction
in a complete promotion gate, and production archive scheduling is unchanged.

Machine-readable trial results and per-archive logs are retained in the run's
`archive-benchmark-results` artifact. Repeat the exact workflow with:

```bash
gh run rerun 39177262190 -R reefyai/reefy
```

Or run `python3 tools/ci/benchmark-debug-archives.py` on the cached runner from
that tool revision. A subsequent run must record its own source/input hashes
because the shared build cache can contain a newer completed firmware build.

## General-pool provider publication validation

[Workflow 39177430397](https://github.com/reefyai/reefy/actions/runs/39177430397)
passed at production workflow revision
`d92b0c35c6e865c9d1474b594e54996d9c574818`. Later review commits add timing
notes only. All four publisher jobs started together on the existing general
runner pool, leaving the heavy compilation runner free. The NVIDIA, AMD,
Intel and synthetic-fixture publications, signatures, attestations, provider
catalog and release-ready check all succeeded.

| Publication | Elapsed |
| --- | ---: |
| NVIDIA | 69s |
| AMD | 100s |
| Intel | 56s |
| Synthetic fixtures | 112s |
| Combined publication span | **112s** |
| Build completion to release-ready completion | **126s** |

The reference workflow `39151726143` had a 296-second post-build release tail;
this validation's tail was 126 seconds, 170 seconds shorter. This is one
observed pair with different source identities/cache and runner conditions,
not a controlled whole-build or full-gate speed benchmark. The service review
branch also releases its host build slot once the main build job succeeds,
while still requiring complete workflow success before target publication.
That permits later compilations to overlap this immutable publication tail.

The provider commits must remain reachable when merging their review PRs:
use merge commits to preserve the exact pinned workflow SHAs, or update and
revalidate the firmware pins after any history rewrite. All 50 existing
provider payload tests passed before live workflow validation.
