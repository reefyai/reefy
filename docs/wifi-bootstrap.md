# Local WiFi bootstrap

During boot, a device without ready Ethernet offers an open Reefy-Setup-XXXX
network when it is unadopted or has no saved WiFi. An adopted device first
allows 60 seconds for saved WiFi association and usable IP configuration,
then offers setup if that fails. Ready Ethernet means carrier and a usable
IP address. No backend, DNS or Internet probe controls this choice.
Join it and open http://192.168.73.1 if the captive page does not open.
Select a WPA/WPA2-Personal network or enter its hidden SSID, then provide
its password. Enterprise authentication and open upstream networks are
outside this initial implementation. The setup AP itself is open.
Reefy stops its AP while trying the upstream network. Association or DHCP
failure restores the setup AP. Cloud outages do not discard valid WiFi.
A reboot before adoption discards credentials and starts setup again.

The setup service owns one WiFi radio, a temporary networkd configuration,
hostapd, dnsmasq, and an interface-bound HTTP server. All temporary settings
live in /run. An adopted device also persists a tested repair in its encrypted
state directory until the backend acknowledges it. It does not bridge or share
internet access. Ethernet becoming ready closes AP. After successful setup,
runtime connectivity loss keeps normal client reconnect behavior and never
opens AP; reboot re-enters the boot decision.

While AP is active, retry saved WiFi every five minutes only when no station
is associated and one full minute has passed without clients. Observe the
actual station list and connect/disconnect events, not DHCP leases. Unknown
station status defers automatic retries. An idle connected phone also defers
retry. A single-radio attempt may suspend AP for up to 60 seconds; restore AP
on failure. The portal Connect action is immediate.

## Approved adoption handoff

The compatibility manifest advertises commands.wifi_bootstrap_credentials
revision 2. Revision 1 remains adoption-only; revision 2 also reports tested,
pending portal settings from an adopted device. Older firmware without this
capability keeps its existing
adoption path. The backend requests credentials before provisioning and
before publishing the first desired state, without holding a DB transaction.

Request on the existing account/device commands topic:

```json
{"action":"wifi_bootstrap_credentials","id":"synthetic-request-id"}
```

Response on that device's commands/response topic, QoS 1, never retained:

```json
{"id":"synthetic-request-id","status":"success","wifi":{"ssid":"SyntheticNetwork","password":"synthetic-password"}}
```

wifi is null when no local bootstrap credentials exist. Responses are bound
to the account, device, and random request ID. Retained and late responses
cannot update configuration. The device does not cache secret responses in
its generic command replay cache or publish them in ordinary status reports.
Timeout or malformed response fails adoption so it can be retried.

Explicit adoption WiFi settings take precedence over bootstrap credentials;
working bootstrap credentials take precedence over clone defaults. The
server stores the selected settings in existing wifi_config fields. After
persisting desired state, the data plane stops the temporary radio owner,
applies normal WiFi configuration, and removes the temporary credential
file only after connection succeeds. Adoption failure before this point
keeps temporary credentials available for retry until reboot.

Automated simulated-radio coverage lives in reefy-service tests/e2e. Real
adapter behavior and Android/iOS portal discovery are tested manually by
the operator for now.

## Adopted network repair

For revision 2, the backend requests pending settings on a live adopted-device
status outside the MQTT subscriber thread. It verifies the account/device
binding, saves the settings with an ownership and concurrent-update check, and
sends matching desired state to acknowledge the repair. It never waits for an
MQTT reply while holding the subscriber's desired-state delivery lock.

The pending local repair is mode 0600, bound to the persistent device UUID,
and survives reboot. It is removed only after matching desired state has been
persisted and normal WiFi configuration succeeds. A stale desired-state push,
including from an older backend, cannot revert a pending tested repair or
interrupt a portal connection attempt. Unadopted credentials remain RAM-only.
No new REST endpoint or pairing authorization is introduced.
