# WiFi power saving

Reefy prioritizes network responsiveness over WiFi idle power savings.
The kernel configuration disables `CONFIG_CFG80211_DEFAULT_PS`, making
power saving off by default for newly created wireless interfaces, including
additional and hotplugged adapters. Drivers can override the kernel default.
After a successful managed connection, `wifi-setup` also runs
`iw dev "$IFACE" set power_save off` to cover those drivers. A failure logs
a warning and does not prevent DHCP or an otherwise successful connection.
Automatic reconnects retain the interface setting; creating a new interface
uses the kernel default again. A driver that overrides that default needs
the setup helper to reapply it when configuring its connection.

This changes the WiFi protocol power-saving default only. It does not disable
PCIe ASPM, runtime PM, NVMe APST, or other driver-specific low-power modes.
Power consumption may increase. No desired-state field or backend rollout
is required; existing installations get the default with updated firmware.

NVMe APST is different: its host policy is an explicit desired-state override,
with reconciliation and removal semantics. Using that mechanism for this
firmware default would delay application until reconciliation and require a
new public policy field. WiFi's kernel default and existing connection helper
apply the behavior at interface creation and managed connection time.

## Why the default is off

A controlled test on an RTL8851BE adapter using the `rtw89_8851be` driver
and Linux 6.18.57 showed that WiFi power saving was the main source of
interactive SSH delay. The connection used 5 GHz WiFi with a strong signal;
initial driver counters reported no transmit retries, transmit failures or
beacon loss. SSH traffic traversed a Cloudflare tunnel, so the measurements
include the complete remote-access path rather than just the wireless hop.
Only the WiFi protocol power-save setting was changed during the experiment.

The test alternated on, off, on, off. Each pass verified the setting with
`iw dev <interface> get power_save`, waited three seconds, then opened a
fresh SSH session. Within that established session, a remote shell echoed
one line per request. The test discarded the first round trip and measured
20 further round trips, with a 1.2-second pause before each request. SSH
connection establishment was excluded from the timing. After closing that
session, a separate gateway test sent 15 pings spaced one second apart.
Gateway probes did not overlap the SSH measurements.

| Pass | WiFi power saving | SSH median | SSH 95th percentile | SSH maximum | Gateway average | Gateway maximum |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | On | 185.22 ms | 296.31 ms | 431.09 ms | 12.250 ms | 126.443 ms |
| 2 | Off | 19.16 ms | 36.18 ms | 41.34 ms | 5.188 ms | 17.321 ms |
| 3 | On | 153.87 ms | 244.29 ms | 303.17 ms | 29.476 ms | 291.850 ms |
| 4 | Off | 19.71 ms | 43.96 ms | 58.05 ms | 3.792 ms | 8.001 ms |

All four gateway tests had zero packet loss. The SSH 95th percentile is
the nearest-rank value, sample 19 of 20 after sorting. Median SSH latency
fell by approximately 87-90% with power saving off. Turning power saving
back on restored the delay, and disabling it again restored low latency.
The complete SSH path reached roughly 20 ms median without changing the
tunnel, which supports addressing WiFi power saving first.

This behavior is consistent with wireless power-saving mechanisms that
put the adapter to sleep during idle periods. See the
[Linux Wireless dynamic power-save documentation](https://wireless.docs.kernel.org/en/latest/en/users/documentation/dynamic-power-save.html).
The experiment establishes the effect of the WiFi power-save setting on
this adapter and connection; it does not identify the exact firmware or
access-point buffering mechanism responsible for each delayed packet.

An earlier repeat overlapped gateway probes with SSH probes. Its results
are excluded from the table because extra traffic changes idle behavior.
The table contains sanitized aggregate measurements, not device identities,
network names, addresses or raw fleet logs.

These are short tests on one adapter and network, not a guarantee of the
same improvement on every WiFi chipset. They justify a responsiveness-first
default for Reefy's network-managed devices while keeping unsupported
adapters usable. Idle power consumption and temperature were not measured;
disabling power saving can increase energy use. The results do not justify
disabling PCIe ASPM, runtime PM or other independent power-saving features.
Firmware boot, reconnect, hotplug and wider adapter coverage remain separate
acceptance checks; runtime toggling does not validate those paths.

## Validation

Run the committed host-only checks:

```sh
python3 -m unittest discover -s board/reefy/reefy/tests -p test_wifi_power_save.py -v
```

The same tests on baseline `cc7aeac8a40bd2aea3a9cdbc772810e57d91eb10`
fail the kernel default assertion and setup assertions for three interface
names and the unsupported-adapter warning. The missing-interface preservation
case passes. With this change, all cases pass. Network tools are mocked;
configuration writes are confined to a temporary directory, and unrelated
configuration and credential-comment removal are checked.

These checks validate configuration and setup behavior, not real radio
latency or the final firmware image. Firmware acceptance should verify
`iw dev <interface> get power_save` after boot, managed connection, automatic
reconnect and adapter hotplug. Check multiple interfaces when available and
confirm unsupported adapters still connect. Compare established SSH echo
round trips with power saving on/off/on/off, pausing between samples, and
run gateway latency probes separately. Do not use continuous ping during
the SSH test because it changes the adapter's idle behavior.
