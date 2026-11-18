# Adoption partition refresh and exact-subject publication

Adoption previously ran whole-disk `partprobe` after creating a key or data
partition. A delayed acknowledgement could stop provisioning even when the
kernel already had the new partition. Adoption now adds only the created
partition with `partx --add --nr`, retaining the ten-second command budget.
Within the previous one-second node-settle budget, it checks the block node,
parent device, partition number, type and geometry against the CRC-validated
GPT. Unknown or conflicting evidence stops before key writes or formatting.
A timeout never authorizes another partition-table mutation or a reprobe.

Buildroot explicitly includes partx. A warm cache missing it refreshes only
util-linux; post-build rejects an image without the executable. Device
credentials, MQTT mapping and authorization are unaffected.

Focused validation:

```sh
python3 -m unittest discover -s board/reefy/reefy/tests -p test_adoption_partition_refresh.py -v
python3 -m unittest discover -s board/reefy/reefy/tests -p 'test_storage*.py' -v
python3 -m unittest discover -s tools/ci -p test_partition_tools.py -v
```

The seven new adoption tests pass. The same adoption/restore journey errors
against the original storage module when its refresh acknowledgement times
out, then passes with the fix without writing a fresh key or formatting an
existing volume. Negative cases reject sibling devices, incorrect partition
numbers, stale geometry, malformed GPT and absent nodes. Both 512-byte and
4096-byte logical sectors are covered. The storage suite passes 198 tests
with four platform skips. Three packaging/cache checks pass.

Native adoption remains exercised by the committed provisioning, reset and
watchdog scenarios in the service E2E suite. Full E2E and production deployment
are intentionally outside this validation.

The firmware workflow also pins the reviewed AMD and Intel publication fixes:

- https://github.com/reefyai/reefy-amd/pull/10
- https://github.com/reefyai/reefy-intel/pull/15

Merge those provider PRs first. Their eight focused tests per provider execute
the actual push step against a synthetic delayed registry and cover strict
refusal cases. They wait for the original immutable digest without repushing;
SBOM attachment, signing and attestation are still required before release.
