# Late storage discovery

Fresh images have no USB encryption-key partition and proceed directly to
bootstrap. The existing single-disk, multi-disk and explicit USB-only adoption
choices are unchanged.

When a key partition exists, boot polls for matching encrypted storage for up
to 900 seconds. It unlocks newly discovered members with the existing key and
reads current LVM membership. All required PVs must be present before the
existing activation and filesystem-validation paths run. An unrelated visible
disk does not release the wait. Already opened mappings are not reopened and
wrong-key disks are not repeatedly subjected to expensive passphrase attempts.

Discovery ends immediately when storage is available. Legacy directly encrypted
USB data filesystems remain supported. Matching encrypted storage whose outer
VG metadata is unreadable reaches the existing guarded metadata-recovery path
once. Polling never runs filesystem repair or activates partial storage. The
separate recovery supervisor's three-hour bound is unchanged.

An expired discovery deadline, malformed membership report, or unsuccessful
persistent mount sets the existing RAM provisioning guard and leaves bootstrap
diagnostics available. No replacement disk is formatted. The next boot retries
normal discovery, so reconnecting an absent member can restore the original
identity and data.

No expected-disk file, EFI configuration or new USB write is introduced.
Existing reset, re-adoption, storage expansion and A/B preservation paths do
not need a new metadata lifecycle.

## Regression coverage

Unit coverage lives in `board/reefy/reefy/tests/test_storage_discovery.py`.
Device journeys remain in the service repository's established
`tests/e2e/scenarios/test_delayed_storage_arrival.py`. The original unchanged
disk-present and delayed-arrival cases demonstrated failure on baseline
firmware while proving on-disk preservation. The same cases must pass on the
candidate, alongside staggered multi-member arrival, actual 900-second timeout,
reset/re-adoption, A/B behavior and unselected-disk preservation. Retain exact
artifact provenance and machine-readable results in PR validation notes.
