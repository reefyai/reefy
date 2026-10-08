# System userspace debug archives

Each firmware workflow produces a separate `reefy-userspace-debug` artifact and
small `reefy-userspace-debug-index` lookup artifact. Main builds retain these
for 90 days; unreleased branches retain them for 14 days. Preserve supported
release symbols outside Actions before expiration. No old archives are deleted
by this change, and no container images are included.

Buildroot compiles target packages with level-2 debug information and still
strips deployed ELF files. A pre-strip target-finalize hook caches unstripped
system executables and libraries by GNU or Go build identity. Warm builds reuse
only exact matching originals. Missing DWARF, missing identities, changed code
or changed code addresses block publication instead of producing a partial
coverage claim. Kernel modules and non-amd64 firmware blobs are excluded; their
kernel debug archive remains separate. Source-mode transitions require a clean
Buildroot toolchain/package rebuild while download and compiler caches remain.

The archive contains:
- A sysroot of actual shipped ELF files and runtime aliases, plus staged
  NVIDIA host-tool executables. These are host tools, not container apps.
- Matching unstripped originals and GNU `.build-id` debugger lookup links.
- C/C++/Rust/Go source files from target-package build trees, plus Python's GDB
  helper and exact Reefy Python sources. Configuration and signing-key files,
  environment dumps, arbitrary build-tree files and host packages are excluded.
- Buildroot configuration, firmware identity, repository identities, an ELF
  coverage inventory, code/content hashes and SHA256SUMS.

The index selects the exact firmware image/build and checks the archive
contents by SHA-256 (this is integrity checking, not independent provenance). The archive prints its measured compressed size in CI.
Shipped build IDs and code sections/addresses must match their original debug
ELF. RPATH normalization is allowed; arbitrary ELF substitutions are not.

After extracting and verifying SHA256SUMS, configure GDB with the archive's
sysroot and debug directory. Set a source-path substitution from metadata's
`source_path_prefix` to the extracted `sources` directory. Use the matching
unstripped executable. Go executables can have Go rather than GNU build IDs;
select their unstripped executable explicitly. These archives are not a
complete compiler SDK and optimized code can inline or optimize away locals.

Target links explicitly enable GNU build IDs with stock `BR2_TARGET_LDFLAGS`.
The CI configuration guard performs a full rebuild when debug mode or target
linker flags change, so cached binaries cannot retain the previous settings.
DWARF alone is insufficient: capture rejects binaries without a GNU/Go identity.

See [collection, traceability and security](../../docs/DEBUGGING-TRUST.md) for
the end-to-end verification chain and current provenance limits.
