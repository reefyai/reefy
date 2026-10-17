# Local WiFi bootstrap

An offline unadopted device advertises an open Reefy-Setup-XXXX network.
Join it and open http://192.168.73.1 if the captive page does not open.
Select a WPA/WPA2-Personal network or enter its hidden SSID, then provide
its password. Enterprise authentication and open upstream networks are
outside this initial implementation. The setup AP itself is open.
Reefy stops its AP while trying the upstream network. Association or DHCP
failure restores the setup AP. Cloud outages do not discard valid WiFi.
A reboot before adoption discards credentials and starts setup again.

The setup service owns one WiFi radio, a temporary networkd configuration,
hostapd, dnsmasq, and an interface-bound HTTP server. All temporary settings
and credentials live in /run. It does not bridge or share internet access.
Existing Ethernet connectivity and adopted devices bypass setup.

## Approved adoption handoff

The compatibility manifest advertises commands.wifi_bootstrap_credentials
revision 1. Older firmware without this capability keeps its existing
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
