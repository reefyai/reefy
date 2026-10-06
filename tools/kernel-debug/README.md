# Exact kernel debug artifacts

Every firmware build uploads `reefy-kernel-debug`, containing
`reefy-kernel-debug.tar.gz`, separately from deployment artifacts. Existing
`reefy-dev` and `reefy-prod` consumers are unchanged. GitHub retention is 90 days;
this is not permanent archival. Download and retain the matching archive for
supported releases before expiry.

The archive contains the exact vmlinux with DWARF/BTF, kernel configuration,
System.map, Module.symvers, generated headers, module inventories, unstripped
base/provider modules, build command records, firmware identity, tool versions,
source revision identities, tracked Reefy build recipes, and SHA256SUMS. Kernel
source URL/ref and Buildroot options are in buildroot.config. It is a debug
bundle, not a complete prepared kernel SDK. Kernel signing private keys and
arbitrary build-tree files are deliberately excluded.

The collector checks the packaged bzImage against the original build output,
extracts the kernel from each EFI and compares it, verifies the boot kernel's
GNU build ID, and matches every shipped base/Intel/NVIDIA/AMD module to an
unstripped original by GNU build ID. Base and Intel modules must retain BTF.
External providers must have DWARF originals; their runtime BTF availability is
recorded rather than assumed. The synthetic test fixture is not included in
this production module inventory.

Full DWARF remains in the archive/build tree for base and Intel modules.
Runtime BTF intentionally adds some image size. Existing external provider
packaging controls their own stripping policy. This change does not turn on
KASAN, lockdep or other high-overhead debugging instrumentation.

Archive module verification and bundle hashing use up to 16 workers. Compression
uses pigz with the same gzip archive format. Timings for indexing, verification,
hashing, and compression are printed in CI; failures still block publication.

The cache key conservatively covers tracked board/config/package/tool inputs
and the pinned Buildroot revision, excluding runtime rootfs-overlay files and
board tests. Build hooks, unknown board paths, and external build definitions
remain covered. Changes force kernel and dependent package
rebuilds. The key is committed to the cache only after successful archive
verification/upload. A toolchain ABI change can still require a completely
clean Buildroot output, as with ordinary Buildroot upgrades; kernel dirclean
alone does not rebuild an existing toolchain.

To inspect a downloaded archive:

```sh
mkdir -p debug-bundle
tar -xzf reefy-kernel-debug.tar.gz -C debug-bundle
cd debug-bundle
sha256sum -c SHA256SUMS
readelf -n kernel/vmlinux
pahole -F btf -C task_struct kernel/vmlinux
```

Match os-release identities and GNU build IDs to the running kernel/modules
before using layouts. A kernel release string alone is insufficient. New
firmware must be validated for boot, provider loading, and runtime BTF before
using it for tracing. Enabling BTF does not itself implement payload hashing,
PRP/SGL capture or comprehensive DMA lifetime tracking.
