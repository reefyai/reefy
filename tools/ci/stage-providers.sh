#!/usr/bin/env bash
set -euo pipefail
# This step compiles the synthetic module against the BTF-enabled kernel.
test -x "$BR_OUTPUT/host/bin/pahole"
test "$(command -v pahole)" = "$BR_OUTPUT/host/bin/pahole"
pahole --version
os_release="$BR_OUTPUT/target/usr/lib/os-release"
kernel_release=$(find "$BR_OUTPUT/target/lib/modules" \
  -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort)
reefy_build_id=$(sed -n 's/^REEFY_BUILD_ID=//p' "$os_release")
kernel_abi_sha256=$(sed -n 's/^REEFY_KERNEL_ABI_SHA256=//p' "$os_release")
image_version=$(sed -n 's/^IMAGE_VERSION=//p' "$os_release")
test -n "$kernel_release"
test -n "$reefy_build_id"
test -n "$kernel_abi_sha256"
test -n "$image_version"

nvidia="$BR_OUTPUT/reefy-artifacts/nvidia"
test -x "$nvidia/toolkit/nvidia-ctk"
test -x "$nvidia/toolkit/nvidia-cdi-hook"
for module in nvidia nvidia-drm nvidia-modeset nvidia-uvm; do
  find "$nvidia/modules-root/lib/modules/$kernel_release" \
    -name "${module}.ko*" | grep -q . \
    || { echo "missing provider input ${module}"; exit 1; }
done
cat > "$nvidia/metadata.env" <<EOF
KERNEL_RELEASE=$kernel_release
REEFY_BUILD_ID=$reefy_build_id
REEFY_KERNEL_ABI_SHA256=$kernel_abi_sha256
IMAGE_VERSION=$image_version
EOF

intel="$BR_OUTPUT/reefy-artifacts/intel"
for module in i915 xe intel_vpu; do
  find "$intel/modules-root/lib/modules/$kernel_release" \
    -name "${module}.ko*" | grep -q . \
    || { echo "missing Intel provider input ${module}"; exit 1; }
done
test -d "$intel/firmware-root/lib/firmware/i915"
test -d "$intel/firmware-root/lib/firmware/xe"
test -d "$intel/firmware-root/lib/firmware/intel/vpu"
cp "$nvidia/metadata.env" "$intel/metadata.env"

amd="$BR_OUTPUT/reefy-artifacts/amd"
for module in amdgpu amdkcl amdxcp amdttm amd-sched \
    amddrm_ttm_helper amddrm_buddy amddrm_exec amddrm_suballoc_helper; do
  find "$amd/modules-root/lib/modules/$kernel_release" \
    -name "${module}.ko" | grep -q . \
    || { echo "missing AMD provider input ${module}"; exit 1; }
done
cp "$nvidia/metadata.env" "$amd/metadata.env"

fixture="$BR_OUTPUT/reefy-artifacts/fixture"
mkdir -p "$fixture"
cp "$nvidia/metadata.env" "$fixture/metadata.env"
kernel_build=''
for candidate in "$BR_OUTPUT"/build/linux-*; do
  test "$(cat "$candidate/include/config/kernel.release" 2>/dev/null || true)" = "$kernel_release" || continue
  test -z "$kernel_build" || { echo "multiple exact kernel build trees"; exit 1; }
  kernel_build=$candidate
done
test -n "$kernel_build"
make -C "$kernel_build" \
  ARCH=x86 \
  CROSS_COMPILE="$BR_OUTPUT/host/bin/x86_64-buildroot-linux-gnu-" \
  M="$GITHUB_WORKSPACE/artifact-fixtures/module" modules
cp "$GITHUB_WORKSPACE/artifact-fixtures/module/reefy_e2e_artifact.ko" \
  "$fixture/reefy_e2e_artifact.ko"

echo "reefy_build_id=$reefy_build_id" >> "$GITHUB_OUTPUT"
echo "kernel_abi_sha256=$kernel_abi_sha256" >> "$GITHUB_OUTPUT"
echo "image_version=$image_version" >> "$GITHUB_OUTPUT"

