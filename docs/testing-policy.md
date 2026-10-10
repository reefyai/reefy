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
