# Boot log hygiene validation

The boot credential helper must apply the saved password hash without
tracing the password or hash, even when invoked by a shell with tracing
enabled. Early WiFi state loading must report parse failures using only
the exception type, preserve valid v1/v2 connection arguments, and leave
the saved state unchanged.

Run the committed synthetic shell integration tests:

```sh
python3 -m unittest discover -s board/reefy/reefy/tests -p 'test_boot_credentials.py' -v
python3 -m unittest discover -s board/reefy/reefy/tests -p 'test_wifi_early.py' -v
python3 -m unittest discover -s board/reefy/reefy/tests -p 'test_wifi_power_save.py' -v
```

The Python tests execute the real shell helper bodies with synthetic state
and safe command fakes. They do not access devices or change host users,
network configuration, or password databases. The workflow discovers these
tests automatically and watches both helper paths for future changes.

The same committed tests were run against upstream baseline
bb42d619cd7e8d9f25e7825a51baaae19dd65168 and the changed helpers. Baseline
credential checks fail for both normal and inherited tracing; malformed
JSON and wrong-shaped WiFi state fail because no diagnostic is emitted.
Valid WiFi loading and missing-state cases pass on both. Changed helpers
pass all cases, including unchanged state and unrelated shadow entries.
Machine-readable results and exact source hashes are in
[boot-log-hygiene.json](validation/boot-log-hygiene.json).

Docker's new daemon default is json-file with max-size=200m and max-file=3:
approximately 600 MiB per container. Existing daemon settings are preserved.
The configuration is parsed and compared with the baseline to check that
only these logging defaults changed. Actual Docker rollover on newly built
firmware has not been exercised by this host-only validation.

Daemon defaults apply to newly created containers without explicit logging
overrides. Existing containers keep their creation-time logging settings;
this change does not recreate them or override Compose logging settings.
