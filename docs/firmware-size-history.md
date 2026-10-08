# Firmware image size history

Record representative released firmware and validated candidates here when
changing Buildroot, kernel configuration, package selection or packaging.
Keep exact byte counts and immutable build references so later comparisons
remain useful after CI artifacts expire. MiB means 1,048,576 bytes.

## Recorded builds

| Firmware version | Buildroot | Source commit | Build |
| --- | --- | --- | --- |
| `2026.10.06-48` (baseline) | 2025.11.3 | `a3e9dc07ed4442cd89adf70cce5b47cd00d26f3c` | [37535850104](https://github.com/reefyai/reefy/actions/runs/37535850104) |
| `2026.10.07-03` (upgrade candidate) | 2026.08 | `3829aabf0efb375a7499d00581ac35d5b018dd9e` | [37645216613](https://github.com/reefyai/reefy/actions/runs/37645216613) |

Both builds use Linux 6.18.54. The candidate combines provisioning/tooling
fixes, the Buildroot upgrade, thin-provisioning-tools 1.3.4 and updated GPU
providers. These are combined-change measurements, not an attribution of
size growth to an individual package. Successful compilation is separate
from E2E and hardware validation.

Sizes below come from the completed builds' output file listings. Dev and
production EFI, RAW and VHD outputs have equal sizes in these two builds.

| Output | Baseline bytes | Candidate bytes | Baseline MiB | Candidate MiB | Change MiB | Change |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| EFI firmware (`reefy-prod.efi`) | 376,529,408 | 389,160,960 | 359.09 | 371.13 | +12.05 | +3.35% |
| RAW disk image (`reefy-prod.raw`) | 2,202,009,600 | 2,202,009,600 | 2,100.00 | 2,100.00 | +0.00 | +0.00% |
| VHD (`reefy-prod.vhd`) | 765,654,016 | 790,825,984 | 730.18 | 754.19 | +24.01 | +3.29% |
| Rootfs CPIO (`rootfs.cpio`) | 1,288,936,448 | 1,330,634,752 | 1,229.23 | 1,268.99 | +39.77 | +3.24% |
| Compressed rootfs (`rootfs.cpio.xz`) | 262,352,236 | 269,005,060 | 250.20 | 256.54 | +6.34 | +2.54% |

The EFI payload increased by 12,631,552 bytes (12.05 MiB, 3.35%). The RAW
image remains fixed at 2,100 MiB by the A/B partition geometry; that size
cannot reveal payload growth. Both boot slots receive the EFI, which also
explains why VHD growth is approximately twice the EFI growth.

GPU provider payloads and debug archives are published separately and are
not embedded in the EFI. Their sizes are recorded below.

## GitHub Actions artifact sizes

These are exact `size_in_bytes` values from the two workflow runs' artifact
API responses. They measure ZIP archives retained by GitHub Actions, not
unpacked files or published OCI payloads. The four `reefy-publish.spdx.json`
archives are summed because they share a name across publisher jobs. Totals
sum every returned artifact once; they do not include logs retained outside
the uploaded log artifact, registry storage or previous runs.

| Artifact | Baseline bytes | Candidate bytes | Baseline MiB | Candidate MiB | Change MiB | Change |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `reefy-prod` | 1,132,845,270 | 1,170,597,670 | 1,080.37 | 1,116.37 | +36.00 | +3.33% |
| `reefy-dev` | 1,132,845,199 | 1,170,597,576 | 1,080.37 | 1,116.37 | +36.00 | +3.33% |
| `manual-debug-shell-efi` | 375,197,435 | 387,808,118 | 357.82 | 369.84 | +12.03 | +3.36% |
| `reefy-kernel-debug` | 3,010,005,395 | 3,026,008,313 | 2,870.56 | 2,885.83 | +15.26 | +0.53% |
| `reefy-nvidia-inputs` | 20,374,521 | 20,807,422 | 19.43 | 19.84 | +0.41 | +2.12% |
| `reefy-amd-inputs` | 291,613,588 | 307,612,832 | 278.10 | 293.36 | +15.26 | +5.49% |
| `reefy-intel-inputs` | 21,272,208 | 22,793,990 | 20.29 | 21.74 | +1.45 | +7.15% |
| `reefy-artifact-fixture-inputs` | 56,769 | 56,757 | 0.05 | 0.05 | -0.00 | -0.02% |
| `reefy-build-logs` | 27,816 | 527,690 | 0.03 | 0.50 | +0.48 | +1797.07% |
| `reefy-provider-catalog` | 938 | 936 | 0.00 | 0.00 | -0.00 | -0.21% |
| `reefy-release-ready` | 193 | 194 | 0.00 | 0.00 | +0.00 | +0.52% |
| `reefy-publish.spdx.json` (4 archives combined) | 14,729 | 15,103 | 0.01 | 0.01 | +0.00 | +2.54% |
| **Total per workflow run** | 5,984,254,061 | 6,106,826,601 | 5,707.03 | 5,823.92 | +116.89 | +2.05% |

Provider `*-inputs` archives are compilation/staging inputs used by publisher
jobs. In particular, their module sizes are not the device's compressed
provider download size. Debug archives remain separate from runtime images.
Differences in uploaded log sizes also reflect cold versus cached builds;
they do not represent a runtime footprint change.

## Published provider payload sizes

These measurements come from the immutable GHCR manifests selected by each
build's provider catalog. Values are compressed SquashFS layer bytes, without
OCI config/manifest, signatures or attestation overhead. Common layers and
exact-kernel layers are separate. Totals count each listed layer once and
are not a measurement of registry deduplication or downloads on a particular
device.

| Provider layer | Baseline bytes | Candidate bytes | Baseline MiB | Candidate MiB | Change MiB | Change |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| nvidia-driver / `common.squashfs` | 322,088,960 | 322,347,008 | 307.17 | 307.41 | +0.25 | +0.08% |
| nvidia-driver / `kernel.squashfs` | 10,846,208 | 10,878,976 | 10.34 | 10.38 | +0.03 | +0.30% |
| **nvidia-driver total** | 332,935,168 | 333,225,984 | 317.51 | 317.79 | +0.28 | +0.09% |
| amd-driver / `common.squashfs` | 23,990,272 | 25,026,560 | 22.88 | 23.87 | +0.99 | +4.32% |
| amd-driver / `kernel.squashfs` | 136,638,464 | 144,130,048 | 130.31 | 137.45 | +7.14 | +5.48% |
| **amd-driver total** | 160,628,736 | 169,156,608 | 153.19 | 161.32 | +8.13 | +5.31% |
| intel-accelerator / `common.squashfs` | 10,797,056 | 12,054,528 | 10.30 | 11.50 | +1.20 | +11.65% |
| intel-accelerator / `kernel.squashfs` | 3,350,528 | 3,391,488 | 3.20 | 3.23 | +0.04 | +1.22% |
| intel-accelerator / `tools.squashfs` | 6,754,304 | 6,754,304 | 6.44 | 6.44 | +0.00 | +0.00% |
| **intel-accelerator total** | 20,901,888 | 22,200,320 | 19.93 | 21.17 | +1.24 | +6.21% |
| **All provider layers** | 514,465,792 | 524,582,912 | 490.63 | 500.28 | +9.65 | +1.97% |

Immutable references used for these measurements:

- nvidia-driver:
  baseline `ghcr.io/reefyai/reefy-nvidia@sha256:2e810dc7532c45a897b4824d235ad7b1de4729caa3c3175bc0c01f2d0d4973ee`;
  candidate `ghcr.io/reefyai/reefy-nvidia@sha256:545799c16e44e295a9005a20e203a11a441ba900bbedc44ee15751b6d5993420`.
- amd-driver:
  baseline `ghcr.io/reefyai/reefy-amd@sha256:bb6051c6c0d18ecf1bd91138f65d1d25013dd2c15bac11a14c0bce20e1158c7a`;
  candidate `ghcr.io/reefyai/reefy-amd@sha256:1ccc925ac201487875124b3e4ec93aaa32f278bd61592285c7763cbb59283975`.
- intel-accelerator:
  baseline `ghcr.io/reefyai/reefy-intel@sha256:1b5b80fd8bab67719df3737cd5d9ec919ea31ec8492617d6b0a0ef255f3b0198`;
  candidate `ghcr.io/reefyai/reefy-intel@sha256:2a68c2907be57909a0b5980454f3adba9e116d73a53d34ba6db7e5fa224cdba0`.

## Recording the next comparison

1. Identify the exact image version, source commit, Buildroot version and
   successful workflow run. Use the same flavor and output type on both
   sides. Do not substitute a later A/B test build for the release baseline.
2. Read exact byte counts from the workflow's output listing, or inspect the
   completed build files with GNU `stat`:

   ```sh
   stat -c '%n %s' "$BR_OUTPUT"/images/reefy-prod.efi \
       "$BR_OUTPUT"/images/reefy-prod.raw \
       "$BR_OUTPUT"/images/reefy-prod.vhd \
       "$BR_OUTPUT"/images/rootfs.cpio \
       "$BR_OUTPUT"/images/rootfs.cpio.xz
   ```

3. Add a build reference and measurement row rather than overwriting history.
   Calculate MiB as bytes / 1,048,576 and percentage growth as
   (new bytes / baseline bytes - 1) * 100.
4. Append the GitHub artifact and published provider layer comparisons too. GitHub's `size_in_bytes` describes the uploaded ZIP artifact,
   not the unpacked image or provider layer. Record the compression/output
   format when comparing those archives. Fetch the artifact inventory with
   `gh api repos/reefyai/reefy/actions/runs/<run-id>/artifacts --paginate`;
   sum duplicate names explicitly. For published providers, use the catalog's
   immutable digest and record each OCI manifest layer's title and `size`.
   Do not infer registry payload sizes from the `*-inputs` ZIP archives.

The CPIO and compressed-rootfs figures are build intermediates, not a
measurement of boot-time RAM consumption or the full composite EFI initrd.
A future size investigation should compare package/file contents from the
same two exact builds before assigning the growth to particular components.
