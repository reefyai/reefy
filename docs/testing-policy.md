# Durable regression E2E coverage

Regression E2E checks must be codified, not left as temporary scripts or
one-off terminal commands. Temporary reproductions are useful for diagnosis,
but do not count as final regression coverage.

1. Reproduce the bug on the unmodified baseline using synthetic fixtures.
2. Commit the scenario, fault injection and assertions to the established
   test suite and runner before final validation.
3. Run the same codified test against the fix, including relevant refusal
   cases and checks that unrelated disks, data and state are preserved.
4. Record repeatable commands, exact firmware/source provenance and the
   baseline/fixed results in PR validation notes or test documentation.
   Retain machine-readable results and relevant logs as normal test artifacts.

The main firmware journeys run in
[reefy-service/tests/e2e](https://github.com/reefyai/reefy-service/tree/main/tests/e2e).
Extend that suite for device lifecycle regressions. Backend-independent
firmware regressions may have dedicated committed runners in that test tree.
Document their narrower scope; do not substitute them for a required complete
user journey.

Preserve default parallel execution of full suites, ordinary shared firmware
reuse, fresh A/B and rollback builds, failure evidence and hardware readiness
assertions. Do not weaken assertions to make a run pass. Never commit real
fleet/customer data, credentials or incident-specific identifiers as fixtures.

The storage recovery lifecycle also requires clean guest poweroff between A/B
boots. A failed early `lvm2-monitor.service` can leave late-unlocked thin pools
registered with dmeventd and stall shutdown. The firmware explicitly sets
`global/locking_dir = "/run/lock/lvm"` through an overlay `lvmlocal.conf`, leaving
the package's `lvm.conf` untouched. This lets LVM create its runtime directory
before tmpfiles without the legacy `/var/lock` symlink. The upstream monitoring
and shutdown commands remain responsible for
registering and unregistering pools. Preserve the lifecycle's clean-poweroff
assertion.


## Console readability

The regular dev firmware uses the native kernel `loglevel=5` setting so routine
notices and informational records cannot scroll away the login status. Warnings
and errors remain visible; records stay in the kernel log. Production retains
its existing quiet policy and the explicit debug-shell flavor remains verbose.
The service E2E console-banner and adopted golden-path checks inject synthetic
notice traffic, require the status to remain readable without a forced redraw,
and verify that kernel records and visible errors are preserved.

## Storage discovery command timeouts

Discovery command timeouts are retried within the existing 900-second membership
budget. They do not shorten that budget or increase individual command limits.
A timed-out VG refresh stays pending before membership is inspected. Invalid
metadata and other unexpected errors still refuse provisioning, and discovery
never formats a disk, repairs a filesystem or activates a partial VG.

Run `python -m pytest board/reefy/reefy/tests/test_storage_discovery.py` for
synthetic command-timeout, full-deadline, unrelated-disk and deferred-refresh
coverage. The same final tests fail against unmodified source
`2f59697d750daadc360cd7a8054ca45cac75b47a` when a bounded command times out.
The real lifecycle remains the established service scenario:

```bash
python tests/e2e/run.py -c configs/reefy-dev.yaml --scenario test_missing_storage_member
```

That journey removes one member after clean poweroff, waits the real policy,
checks guarded bootstrap and boot-record preservation, then reattaches storage
and verifies the adopted identity and synthetic data. Keep its 900-second
assertion and all recovery checks. Retain JUnit, lifecycle JSON and guest serial
logs under the normal test results directory with exact firmware provenance.
