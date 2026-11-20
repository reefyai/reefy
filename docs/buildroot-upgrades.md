# Buildroot release upgrades

## Current combined release candidate

Linux is pinned to the latest 6.18 LTS patch release, 6.18.64, with matching
kernel/header archive hashes from kernel.org. The actual source archive was
checked against that SHA256. All 24 driver files in the Intel Wi-Fi policy are
byte-identical to the preceding policy; only its kernel reference changes.
The reproducible-BTF patch applies without fuzz. Provider/kernel ABI checks
and hardware qualification remain required; matching Wi-Fi inputs do not
establish GPU compatibility or repair the earlier storage-contention stalls.

The latest stable Buildroot release is 2026.08.2. Pin the exact upstream
2026.08.x maintenance snapshot `bbbdab36565d4941b6b0449a0eedb5aac8a10c2e`,
which contains that release (`cd8011d7c9965e1bcfc9c18d4272943f9c333b68`)
plus subsequent Go and glibc security updates. Do not rewrite an upstream
tag or track the branch by name. The 2026.11 series is still a release
candidate and is not selected.

| Component | Previous candidate | Combined candidate |
| --- | --- | --- |
| Linux LTS | 6.18.63 | 6.18.64 |
| Buildroot | 2026.08.1 plus Go backport | 2026.08.2 plus pinned stable maintenance |
| Go | 1.27.3 | 1.27.4 |
| glibc | 2.44-57-g34106fee6 | 2.44-77-g438677ef4 |
| systemd | 258.7 | 258.12 |
| Python | 3.14.8 | 3.14.10 |
| certifi | 2026.10.18 plus local restoration | 2026.10.23 |
| pip | 26.1 plus CA backports | 26.2.2 |

The upstream snapshot already includes the Go security update, so retire
the local 1.27.3 patch. `tools/ci/prepare-buildroot.py` now verifies the exact
revision and rejects tracked checkout changes without modifying recipes.
The newer certifi, pip and ensurepip inputs also replace the old CA backports;
retain the final installed-bundle refusal checks and private MQTT trust.
See [the TLS provenance notes](public-tls-trust.md) for their different
upstream CA snapshots and the retired restoration patch.

These updates are appended to the existing storage/watchdog/build fixes in
the same release PR. The preceding passing dev qualification remains evidence
for its original source only. The combined candidate needs a new successful
firmware/provider build, full six-worker E2E and hardware qualification before
promotion. Source preparation performs no deployment or live storage change.

Sources: [kernel releases](https://www.kernel.org/),
[Buildroot releases](https://buildroot.org/download.html),
[pinned stable history](https://gitlab.com/buildroot.org/buildroot/-/commits/bbbdab36565d4941b6b0449a0eedb5aac8a10c2e).

## Earlier base-release migration

The original base migration replaced 2025.11.3 with upstream 2026.08
(`d5180309b1b66ef3b8eaccca70ad69be8e0729a1`).
Buildroot now supplies python-varlink 32.1.0, so the duplicate external
recipe is removed without changing the Varlink runtime version.
Ansible stays at 2.20.3. Relax only its build metadata from
`wheel == 0.45.1` to `wheel >= 0.45.1`, and declare the setuptools and wheel
host dependencies explicitly. An isolated build with setuptools 80.9.0 and
wheel 0.47.0 passes with normal dependency checks enabled. No private wheel
package or global host-tool downgrade is used.
That migration kept Linux explicitly pinned to 6.18.54. Kernel/provider build identity
checks still apply; a toolchain change can change the kernel ABI digest even
when the Linux version is unchanged.

Selected package changes:

| Component | Previous release | 2026.08 |
| --- | --- | --- |
| Go | 1.25.7 | 1.26.6 |
| systemd | 257.10 | 258.7 |
| xfsprogs | 6.11.0 | 6.19.0 |
| LVM2 | 2.03.31 | 2.03.31 |
| thin-provisioning-tools (Reefy recipe) | 1.3.2 | 1.3.4 |

The newer xfsprogs includes upstream fixes released after 6.11.0. This does
not establish that any particular intermittent filesystem-check failure is
fixed; retain the existing non-modifying checks and regression assertions.

The external thin-provisioning-tools recipe upgrades to 1.3.4 for upstream
fixes to damaged-metadata bounds/offset checks and metadata pack/unpack
flushing. The check/dump/repair command definitions and thin superblock
structure are unchanged from 1.3.2. Verify the generated Cargo archive with
Buildroot's pinned Cargo toolchain and locked dependencies; do not reuse a
checksum from an older Cargo archive format or disable download verification.

## open-vm-tools compatibility

Keep open-vm-tools enabled. Buildroot's 11.3.5 source has a known callback
declaration conflict under GCC 15's new default C23 mode. Compile only this
package with `-std=gnu17` through
`board/reefy/reefy/package-overrides/openvmtools.mk`, preserving the preceding compiler's C dialect
without changing the vendor source. The workflow invalidates an existing
package configuration that did not include this mode.

[Upstream issue #750](https://github.com/vmware/open-vm-tools/issues/750)
records the same failure. [The 13.0.0 release notes](https://github.com/vmware/open-vm-tools/blob/stable-13.0.0/ReleaseNotes.md)
confirm its source fix. Remove the workaround once the upstream Buildroot
recipe uses a fixed release. Keep the newer GCC for all other packages.

## GPU provider refresh

Update NVIDIA's R595 production driver to 595.104.02 with matching open
modules, GSP firmware and userspace, plus Container Toolkit 1.20.1. The
provider publisher is pinned to the reviewed source in
[the NVIDIA update PR](https://github.com/reefyai/reefy-nvidia/pull/2).
Verified installer inputs and payload layout do not establish GPU runtime
compatibility; exact-kernel compilation and hardware validation remain gates.

Update the AMD driver and matching firmware to 31.60 through
[the AMD update PR](https://github.com/reefyai/reefy-amd/pull/3), retaining
Container Toolkit 1.3.0 and the current official AMD-SMI package. Package
sizes and hashes match AMD's published Ubuntu 24.04 apt metadata. Validate
the stock source against the candidate Linux 6.18.64 and the supported GPUs.

## Build cache and image versions

Do not reuse a previous Buildroot release's output tree. The firmware
workflow uses `/buildroot-cache/output-buildroot-2026.08.2-bbbdab36`, retaining the
shared download and compiler caches. The old output directory is preserved
for independent validation of the preceding release.

The existing image version format remains unchanged. Serialized CI builds
allocate the next sequence from the highest current-day counter in the new
output, the legacy output and a shared counter outside the output trees.
The allocator mirrors the result back to the legacy counter so alternating
release branches cannot reuse an already allocated version. Invalid counters
or dates going backwards stop publication. Firmware jobs must remain
serialized on the build runner.

## systemd EFI runtime support

Enable `BR2_PACKAGE_SYSTEMD_EFI=y` for Reefy's UEFI firmware. Without it,
systemd 258.7's `efi_has_tpm2()` fallback returns `-EOPNOTSUPP` from a Boolean
function, which evaluates to true. With TPM userspace support enabled, the
TPM generator then waits for absent TPM devices and delays system
initialization by the default 90-second device timeout.

Enabling the stock EFI runtime support allows actual firmware TPM detection.
Keep TPM support and reboot assertions intact; do not mask the TPM target,
increase test deadlines to hide the delay or patch vendor C source. This
option does not enable installation of systemd-boot as Reefy's bootloader.

Buildroot does not automatically invalidate a configured package when its
options change. The CI systemd configuration guard compares the generated
`ENABLE_EFI` value with the selected option, clears a stale systemd package
before building and verifies the compiled configuration afterward. Merely
regenerating `.config` can otherwise ship a new image with the old systemd
binary.

Validate the built image on a UEFI QEMU guest without a TPM: firmware TPM
support must be reported absent and boot must not wait for `dev-tpm0.device`
or `dev-tpmrm0.device`. Retain multi-boot journal, identity and application
recovery checks. The observed reboot time also includes late shutdown and
firmware loading; removing this startup delay does not establish that all
other reboot delays are resolved.

## Validation

Run the unchanged-release fixes first, then the combined release-upgrade
candidate. Avoid overlapping full software suites or running a cold firmware
build alongside a six-worker suite on the same host. Preserve shared firmware
fixtures and fresh A/B and rollback builds. Record each exact firmware and
harness revision, package versions, scenario results and elapsed time.

Before promotion, verify:

- The Linux build config accepts the defconfig without legacy selections or
  unmet dependencies, and requested packages remain enabled.
- Firmware compilation and all exact-build provider publications succeed.
- The full six-worker QEMU suite passes, including provisioning, persistent
  journal history, storage checks, offline cached startup, A/B and rollback.
- Hardware gates cover the supported GPU providers before a release upgrade
  is promoted to production.

See upstream's [release notes](https://buildroot.org/news.html) and
[migration guidance](https://buildroot.org/downloads/manual/manual.html#_migrating_to_new_buildroot_versions).

## Provisioning discovery under parallel load

The one-time existing-volume `vgscan` uses a bounded 60-second deadline.
Record elapsed time on success. Before terminating a timed-out scan, record
its process state, kernel wait channel/stack and CPU, memory and I/O pressure,
plus bounded command output. Discovery errors stop provisioning; do not
retry blindly or proceed to new data-volume creation after an incomplete scan.
This does not change six-worker E2E execution or the overall adoption deadline.
A longer command budget accommodates transient startup contention but does
not establish the cause of a previous timeout.
