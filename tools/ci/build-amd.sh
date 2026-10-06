#!/usr/bin/env bash
set -euo pipefail
# Standalone provider builds need the same host tools as Buildroot.
export PATH="$BR_OUTPUT/host/bin:$BR_OUTPUT/host/sbin:$PATH"
test -x "$BR_OUTPUT/host/bin/pahole"
test "$(command -v pahole)" = "$BR_OUTPUT/host/bin/pahole"
pahole --version
expected_kernel=$(sed -n 's/^BR2_LINUX_KERNEL_CUSTOM_REPO_VERSION="v\([0-9.]*\)"$/\1/p' configs/reefy_defconfig)
test -n "$expected_kernel"
kernel_build=''
for candidate in "$BR_OUTPUT"/build/linux-*; do
  test "$(cat "$candidate/include/config/kernel.release" 2>/dev/null || true)" = "$expected_kernel" || continue
  test -z "$kernel_build" || { echo "multiple exact kernel build trees"; exit 1; }
  kernel_build=$candidate
done
test -n "$kernel_build"
driver_url=$(python3 -c 'import json; print(json.load(open("amd-provider/versions.json"))["amd_gpu_driver"]["package_url"])')
mkdir -p "$BR_OUTPUT/reefy-artifacts/amd"
bash tools/ci/download-package.sh "$driver_url" \
  "$BR_OUTPUT/reefy-artifacts/amd/amdgpu-dkms.deb"
python3 amd-provider/scripts/build_modules.py \
  --driver-package "$BR_OUTPUT/reefy-artifacts/amd/amdgpu-dkms.deb" \
  --kernel-dir "$kernel_build" \
  --kernel-release "$expected_kernel" \
  --toolchain-prefix "$BR_OUTPUT/host/bin/x86_64-buildroot-linux-gnu-" \
  --output "$BR_OUTPUT/reefy-artifacts/amd/modules-root"
