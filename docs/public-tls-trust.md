# Public TLS trust remediation

## Trust boundaries

The personalized MQTT CA chain is `/mnt/reefy/mqtt/ca.crt`. It contains the
private Reefy root and user intermediate and is explicitly loaded by Paho.
It is independent of the host public HTTPS trust store.

Host curl loads `/etc/ssl/certs/ca-certificates.crt` and `/etc/ssl/certs`.
Python's default context uses its OpenSSL default paths, including the hashed
certificate directory. Docker and other Go host clients use system roots.
Containers have their own filesystem and trust stores unless explicitly
mounted from the host. Updating host firmware does not update their bundles.

## Fix

Pin Buildroot to stable maintenance commit
`d6061d653d7fb215c2409c59e4bfad1b9d677215` from `2026.08.x`.
This advances the original 2026.08 release by 274 maintenance commits,
including Go 1.27.2, OpenSSL fixes and the switch from c_rehash to openssl
rehash. This is a broader maintenance update, requiring a fresh firmware
build and lifecycle validation. It does not track a moving branch or master.

Keep ca-certificates 20260223 with its original source checksum. Backport
the verbatim Buildroot-submitted `0002-remove-eMudhra-parents.patch`,
which blacklists "emSign ECC Root CA - G3" and "Comodo AAA Services root".
Buildroot applies this data-only patch through BR2_GLOBAL_PATCH_DIR.
All unrelated root certificates must remain byte-for-byte identical.

Before configuring Buildroot, run `python3 tools/ci/prepare-buildroot.py`.
It verifies the maintenance revision and patch checksum, then applies the
verbatim upstream Go security commit
`4e0cd646932be3631f87f37ee08e5d6c09e2565f`. That patch changes only the
Go version to 1.27.3 and official download checksums. It can be run again
on an already patched checkout and refuses conflicting package changes.
The submodule remains pinned to an upstream commit; the applied recipe diff
is intentional and reproducible from the tracked upstream patch.

Remove Reefy's Go version, GOROOT, compiler-path and hash overrides, plus
the custom installation-link hook. Go now uses upstream's standard
`host/lib/go` installation and `host/bin/go` entrypoint. There are no local
patches to Go compiler, standard-library or TLS source.

CI uses a new output directory for this maintenance update so the 274-commit
advance is validated without stale installed packages. Downloads and ccache
remain reusable. No device update or production deployment is part of this PR.

CI always refreshes the small CA package so an existing .stamp_patched cannot
skip a newly backported security patch. This package is cheap to regenerate;
all unrelated ordinary build-cache reuse stays intact.

A warm package installation must first replace its package-owned Mozilla
certificate directory. Otherwise, retired roots from the old version remain
on disk and are silently concatenated into the new public bundle. Leave local
trust additions and private MQTT credentials untouched.

Buildroot does not automatically rebuild consumers after changing Go.
`tools/ci/go-cache.py` invalidates the configured reverse dependencies of
host-go when the pin changes, including Docker, containerd, runc and
Tailscale. It records reuse only after a successful build and checks the Go
build metadata of every Go binary in the final target tree. Compiler changes,
interrupted refreshes and stale binaries are covered by unit tests. This
preserves ordinary warm-cache reuse after a verified successful build.

## Go generator compatibility

Mgmt 1.0.2's funcgen uses x/tools v0.41.0 to load standard-library types.
Go 1.27.2+ writes unified export data V5, which that importer cannot read.
A clean synthetic html-package load reproduces the earlier firmware failure:
`package "sync" without types was imported from "html"`. x/tools v0.48.0
also rejects V5; the first compatible release is v0.50.0, containing upstream
commit `89ed5c340cb6d4a9437f801cfc718ac5980c938d`.

Keep mgmt 1.0.2 and pin x/tools v0.50.0 in its existing vendoring hook before
`go mod tidy`. This uses the upstream importer with its required module
dependencies. The actual generator and firmware packages must still compile.
`test_go_loader.py` runs the same synthetic package load with both dependency
versions under the real selected Go compiler. It requires baseline failure,
fixed success, and rejection of a missing package. CI runs it after compiler
installation with an explicit module proxy and checksum database for the
isolated test dependencies, and retains compiler/source provenance, module sums and outcomes
in `reefy-go-loader-results.json` alongside the target binary report.

```sh
REEFY_TEST_GO="$BR_OUTPUT/host/bin/go" \
REEFY_TEST_GO_RESULTS="$BR_OUTPUT/reefy-go-loader-results.json" \
python3 -m unittest discover -s tools/ci -p 'test_go_loader.py'
```

## Upstream alignment

The CA patch file is copied byte-for-byte from Florian Larysch's Buildroot
submission, acknowledged by Yann E. MORIN. It is an upstream-submitted patch,
not yet merged as of this review. Keep its filename and contents unchanged.

The stable maintenance recipe and exact Go security backport replace the
previous local Go configuration. The preparation step runs in both firmware
CI jobs and is documented for local builds. The cache helper reads GO_VERSION
from the patched upstream recipe and clears the package-owned standard Go
root before reinstalling a changed compiler. This prevents removed Go source
files from surviving an in-place installation, while preserving unrelated
host files. Both direct and nested Go invocations use upstream's single root.

Sources:
- https://lists.buildroot.org/pipermail/buildroot/2026-October/813828.html
- https://lists.buildroot.org/pipermail/buildroot/2026-October/813882.html
- https://gitlab.com/buildroot.org/buildroot/-/commit/4e0cd646932be3631f87f37ee08e5d6c09e2565f
- https://deb.debian.org/debian/pool/main/c/ca-certificates/ca-certificates_20260816+nmu1.dsc
- https://go.dev/dl/?mode=json&include=all
- https://github.com/golang/go/issues/82345
- https://emudhra.we-have-a-problem.com/

Only the two affected public trust paths are removed. HTTPS services
requiring them will fail validation. The MQTT trust chain, other roots and
public Reefy interfaces are unchanged.

## Validation

```sh
python3 tools/ci/prepare-buildroot.py
python3 -m unittest discover -s tools/ci -p 'test_*.py'
```

Build-tool unit tests cover the actual stable Go recipe before and after the
verbatim backport, repeat application, a conflicting recipe, a modified patch,
and an incorrect submodule revision. Cache regressions cover stale compiler
source removal, consumer invalidation, unchanged-cache reuse and stale binary
rejection. Synthetic local host files and private MQTT CA sentinels verify
preservation. Run the preparation step before these tests so the Buildroot
submodule is initialized at the selected maintenance revision.

The durable chain probe and QEMU scenario are in the companion service PR:
`tests/e2e/tls_trust/probe.py` and `test_public_tls_trust`. The offline package
check uses the exact baseline Debian 20260223 source (SHA-256
2fa2b00d4360f0d14ec51640ae8aea9e563956b95ea786e3c3c01c4eead42b56)
and the same source plus the committed blacklist backport (patch SHA-256
77d5bf924b807fa6e9cb766fbd597513656dba2c7ab1a14d9feacdbe24e982a6).
The upstream emergency source used as patch provenance is 20260816+nmu1
(SHA-256 914416a1e44a25733aff51092fe4e27fdec1c9f6e8d1db0b8364922486153134).
Both are built with their unmodified makefiles, installed into isolated test
root trees and rehashed with OpenSSL. Synthetic MQTT CA sentinels are added
solely to check preservation.

The runner also requires all unrelated root certificate bytes to be identical
between baseline and patched output. Baseline: six compromised chains remain accepted through both the bundle and
hashed directory (12 of 24 refusal checks fail). Fixed: all 24 refusal checks
pass. Both runs also require valid-chain acceptance under an explicit test
anchor, wrong-hostname rejection, and altered-signature rejection. Validation
times are derived from each public fixture's validity overlap, so certificate
expiry cannot produce a false pass.

Keep JSON reports, source-build logs and JUnit output in the service runner's
normal results/artifact directory. An offline package test does not claim a
completed firmware build, QEMU boot, hardware run or production deployment.
The firmware CI separately builds all flavors and checks actual Go binary
provenance before marking its cache reusable.
