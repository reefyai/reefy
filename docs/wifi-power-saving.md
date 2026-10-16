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
