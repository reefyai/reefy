# Crash diagnostics, symbols and trust

This guide describes the crash-capture and system-symbol implementation proposed
in the persistent-coredump and system-userspace-debug changes. Kernel debug
archives already exist. Do not assume a device has userspace capture or symbols
until its exact firmware has passed publication and runtime validation.
Container applications and their image symbols are outside this guide's scope.

## Collection and storage

Stock systemd-coredump handles eligible userspace fatal signals. It writes crash
metadata to journald and compressed external ELF cores. It does not capture
kernel panics, power loss, controller firmware or arbitrary hung processes.
Kernel symbols support kernel log/tracing analysis; this feature does not enable
kdump or produce a kernel memory dump.

Before writable supported data storage mounts, `/var/lib/systemd/coredump` binds
to a private 256 MiB tmpfs at `/run/reefy-coredumps`. Early capture has a 128 MiB
pool budget and 64 MiB uncompressed per-core limit. RAM-only evidence is lost
on reboot or power loss.

After attachment, the same canonical path binds to
`/mnt/reefy-data/coredumps`, outside Docker's data root and configuration state.
The helper chooses the smaller of 4 GiB, 5% of available space and available
space minus a 512 MiB reserve. A budget below 64 MiB leaves RAM fallback active.
The uncompressed per-core cap is the smaller of 2 GiB and half that pool budget.
The helper refreshes the budget when it attaches or retries. Native size and
free-space limits are not a hard filesystem quota and can temporarily overshoot.
The tmpfiles cleanup age is 14 days, not a guarantee of 14 days of history.

Completed early `core.*` files are copied with metadata, fsynced and atomically
published. The helper fsyncs the destination directory before deleting the RAM
source. Ambiguous existing destinations retain the source rather than overwrite
it. Early workers can finish in their old mount namespace; a one-minute timer
retries attachment and drains those late files. Attachment failure does not
block boot, and unrelated mounts or symlink crash directories are refused.
These safeguards cannot prevent loss from failing storage, truncation or a
crash before the original file was durably captured.

The default for systemd services is `CoredumpFilter=elf-headers`. Anonymous heap
and stack mappings and ordinary mapped-file contents are omitted. Register
notes, mapping metadata, ELF headers and the vDSO remain. A faulting instruction
can often resolve to a source line; a complete stack backtrace usually needs
stack memory. The customer or their authorized administrator may explicitly use `CoredumpFilter=default` for one
unit during an approved diagnostic session. That captures private memory.
Service limits, Linux dumpability and process overrides can prevent or change
capture. Filtering is inherited by child processes; it is not a universal
redaction guarantee for every process.

See [crash storage details](CRASH-DUMPS.md) and
[persistent journal history](persistent-journal.md).

## Build-time symbol collection

| Bundle | Contents and verification | Actions retention |
| --- | --- | --- |
| `reefy-kernel-debug` | Exact vmlinux with DWARF/BTF, configs, maps, generated headers, base/provider module originals, source identities, recipes and toolchain identity. Extracted EFI kernels match the build output; shipped modules match original build IDs. BTF availability is recorded. | 90 days |
| `reefy-userspace-debug` | Shipped amd64 system ELF sysroot, pre-strip originals, GNU build-ID lookup links, source inventory, exact Reefy Python scripts, resolved configuration and firmware/repository identities. GNU/Go identities and code bytes/addresses must match. Missing DWARF or identity blocks capture. | Main: 90 days; branches: 14 days |
| `reefy-userspace-debug-index` | Exact image version, Reefy build identity, archive SHA-256, compressed bytes and ELF count. | Main: 90 days; branches: 14 days |

Debug data stays outside deployed firmware images. Target packages compile with
level-2 DWARF and GCC's configured default GNU build IDs, then runtime ELFs
are stripped. Buildroot passes `--enable-linker-build-id` through
`BR2_EXTRA_GCC_CONFIG_OPTIONS`; there is no global GCC-style `LDFLAGS` injection.
The pre-strip cache only reuses matching originals; debug/compiler-linker mode
changes trigger a clean package/toolchain build. Go identities can differ from
GNU identities. Kernel modules and non-amd64 blobs use separate handling.
Staged NVIDIA host-tool executables belong to the system archive, not container
images. Do not infer source availability for closed vendor code from the
presence of kernel-module symbols.

The userspace source collector includes selected C/C++/Rust/Go source files
from installed target-package build trees and Python debugger support. It is
not a complete SDK, full source distribution or guarantee that every DWARF
source path is available. Use each bundle's inventory and metadata to determine
actual coverage. Optimized code can inline functions or remove variables.
See [kernel bundle format](../tools/kernel-debug/README.md) and
[userspace bundle format](../tools/userspace-debug/README.md).

## Traceability and verification

Preserve this chain for an investigation:

1. Record the exact device `IMAGE_VERSION` and `REEFY_BUILD_ID` from
   `/usr/lib/os-release`, the crash boot ID, executable path and core filter.
   Treat real identifiers and raw output as private incident evidence.
2. Select a successful firmware workflow in the trusted `reefyai/reefy`
   repository. Record its run URL/ID, commit SHA and downloaded artifact IDs.
   A version label, branch name or kernel release alone is not sufficient.
3. Verify firmware image provenance and its digest. The workflow currently
   attests the dev/prod EFI and raw images using GitHub's provenance action.
   It does **not** currently attest the kernel/userspace debug archives or
   userspace index. A successful run alone is not a signed symbol statement.
4. Match the userspace index's image/build identities to the device, then its
   `archive_sha256` to the downloaded tarball. Verify each bundle's
   `SHA256SUMS` after safe extraction. Compare bundle metadata with the workflow
   commit and pinned Buildroot/provider source identities.
5. Match the kernel and module GNU build IDs, or userspace GNU/Go build identity,
   against the actual crashed/running binaries. Verify shipped binary hashes
   against the userspace inventory. Use matching shared libraries as well as
   the executable. Never substitute a nearby release's symbols.
6. Keep a private evidence manifest recording digests, acquisition method,
   selected core, matching bundles, analysis tool versions and any truncation,
   missing files or source gaps. A later local digest detects changes after
   acquisition; it does not retroactively prove device authenticity.

Example downloads, run on a trusted analysis host with a synthetic run ID:

```sh
umask 077
mkdir -p investigation/downloads
cd investigation/downloads
gh run download 123456789 --repo reefyai/reefy \
  --name reefy-kernel-debug --dir kernel
gh run download 123456789 --repo reefyai/reefy \
  --name reefy-userspace-debug --dir userspace
gh run download 123456789 --repo reefyai/reefy \
  --name reefy-userspace-debug-index --dir index
sha256sum userspace/reefy-userspace-debug.tar.gz
# Compare with index/reefy-userspace-debug.json before extraction.
# For a separately downloaded firmware image:
gh attestation verify /path/to/verified-image.efi --repo reefyai/reefy
```

Extract trusted, verified bundles into separate private directories without
root privileges using an extractor that rejects unsafe paths and links. Verify
`SHA256SUMS` from each extracted bundle's root. Treat archives as input data;
do not execute included scripts or enable debugger auto-loading implicitly.

SHA-256 and build IDs establish matching/corruption checks, not authorship or
reproducible-build proof. A compromised build runner can produce internally
consistent bad firmware and symbols. Provenance identifies the workflow and
subject digest; it does not certify source safety. Independent debug-archive
attestation and reproducible-build comparison are future hardening steps,
not existing guarantees. Core files and local journal records are not signed
by this collection feature.

## Authorized on-device inspection and analysis

Access the device and export evidence only with customer authorization. These
commands run locally on the authorized device; no automatic upload is enabled:

```sh
sudo findmnt -M /var/lib/systemd/coredump
sudo systemctl status reefy-coredump-spool reefy-persistent-coredump
sudo coredumpctl list
# Explicit backing directory includes prior ephemeral machine identities:
sudo coredumpctl --directory=/mnt/reefy-data/journal list
sudo coredumpctl --directory=/mnt/reefy-data/journal \
  info COREDUMP_UNIT=synthetic-crash.service
```

Select one exact crash record before dumping. A unit can have several records;
unit-only selection is not a unique evidence identifier. Preserve its metadata
privately and check whether storage is missing, expired or truncated. Core and
journal retention differ, so metadata can outlive a core and vice versa.
After approved export, hash the core on acquisition and keep the original
unchanged. Decompress into a private working copy through `coredumpctl dump`.

On an isolated analysis host, use the matching unstripped executable:

```sh
gdb -nx -iex 'set auto-load off' \
  -ex 'set sysroot /private/analysis/userspace/sysroot' \
  -ex 'set debug-file-directory /private/analysis/userspace/debug' \
  -ex 'set substitute-path /original/build/output /private/analysis/userspace/sources' \
  /private/analysis/userspace/unstripped/usr/bin/synthetic-app \
  /private/analysis/synthetic.core
```

Use `metadata.json`'s actual `source_path_prefix` instead of the synthetic
original path. With minimal cores, inspect `info registers`, `info symbol $pc`
and `info line *$pc`; do not promise a full `bt`. With an explicitly approved
full core, a matching Python executable/library and exact Python GDB helper can
support source backtraces. Enable that helper deliberately after review.
Kernel layouts require the matching vmlinux/module originals; BTF availability
alone does not implement tracing probes or DMA/payload instrumentation.

## Security, retention and validation

Crash dumps remain on the customer's device and are not automatically uploaded
to Reefy servers or third parties. The customer or their authorized
administrator controls whether to export and share diagnostic evidence.
This statement concerns crash dumps and their local diagnostic records, not
other device telemetry. Debug-symbol archives are separate build artifacts
and contain no collected device memory.

Crash directories are root-owned mode 0700. Native file access controls still
apply; protect extracted cores, analysis output and backups separately. Minimal
memory capture reduces exposure but does not anonymize registers or metadata.
The stock collector records process command lines and environment in journald
independently of the filter. Both cores and journals can contain secrets and
customer data. Never attach raw cores, journals or real device identifiers to
public issues, source trees, test fixtures or PRs. Publish only reviewed,
sanitized technical summaries and synthetic reproductions.

Symbol archives are code/build artifacts, not runtime memory dumps. Collectors
use selected files rather than arbitrary build-tree copies; signing private
keys, environment dumps and container images are not intentionally collected.
Source files, resolved build configuration and paths still require disclosure
review. This guide does not guarantee that arbitrary source/configuration is
secret-free. Archive access follows repository/Actions permissions. Supported
release bundles must be copied to approved long-term storage before Actions
expiry; no automatic permanent archive or old-symbol deletion is implemented.
Keep exact symbols for every firmware release still supported in the fleet.

Validation must establish all of the following before release:

- Native tests reject missing DWARF/identity and mismatched code, survive runtime
  stripping, resolve a synthetic core to source, and omit a synthetic heap
  payload under the minimal filter.
- Firmware publication verifies kernel/module originals and the complete
  selected installed userspace ELF inventory, rather than silently omitting
  a failed binary.
- Regular QEMU coverage captures a crash before actual data mount, transfers
  it after adoption, captures on persistent storage, retains it after reboot,
  checks the default minimal profile, and resolves an explicit synthetic full
  core with the symbols downloaded for that exact firmware.
- Full parallel software coverage and the hardware gate pass on the reviewed
  candidate. A passing native unit test is not proof of shipped-image behavior.

Record validation references with the release. The current candidate's complete
runtime/source validation is pending; do not turn planned checks into claims
of demonstrated production coverage.
