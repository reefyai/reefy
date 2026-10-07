# Buildroot release upgrades

The Buildroot submodule is pinned to the upstream 2026.08 release
(`d5180309b1b66ef3b8eaccca70ad69be8e0729a1`), replacing 2025.11.3.
Buildroot now supplies python-varlink 32.1.0, so the duplicate external
recipe is removed without changing the Varlink runtime version.
Ansible stays at 2.20.3. Relax only its build metadata from
`wheel == 0.45.1` to `wheel >= 0.45.1`, and declare the setuptools and wheel
host dependencies explicitly. An isolated build with setuptools 80.9.0 and
wheel 0.47.0 passes with normal dependency checks enabled. No private wheel
package or global host-tool downgrade is used.
Linux remains explicitly pinned to 6.18.54. Kernel/provider build identity
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
