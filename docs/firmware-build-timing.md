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
