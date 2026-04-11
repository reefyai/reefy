# MQTT Configuration for SBNB

This document describes how to configure MQTT+mTLS communication for SBNB devices as an alternative or addition to Tailscale connectivity.

## Overview

SBNB supports MQTT with mutual TLS (mTLS) authentication for secure device-to-console communication. This enables:

- **Event-driven configuration management** - devices receive commands and configurations without polling
- **Self-hosted deployments** - run your own MQTT broker instead of relying on external services
- **Hybrid connectivity** - use both Tailscale and MQTT simultaneously
- **Portable credentials** - carry MQTT configuration on USB flash drive

## How It Works

1. **Bootstrap Phase**: Device uses shared certificate from USB to register with broker
2. **Provisioning Phase**: Console assigns UUID and device-specific certificates
3. **Operational Phase**: Device receives configurations and commands via MQTT topics

The reconciler service runs continuously, listening for configuration updates and applying them automatically.

## USB Flash Structure

Place MQTT configuration on your USB flash drive alongside Tailscale keys:

```
/mnt/sbnb/
├── tailscale/
│   └── tskey-*.txt          # Optional: Tailscale auth key
└── mqtt/                     # MQTT configuration bundle
    ├── mqtt.conf             # Broker URL and settings
    ├── ca.crt                # CA certificate (for server verification)
    ├── bootstrap.crt         # Shared bootstrap certificate
    └── bootstrap.key         # Shared bootstrap private key
```

## Configuration File Format

Create `mqtt.conf` with your broker settings:

```ini
# MQTT Broker Configuration
MQTT_BROKER=mqtt.example.com
MQTT_PORT=8883
MQTT_KEEPALIVE=60

# Certificate paths (relative to /etc/sbnb/mqtt/ or /mnt/sbnb-data/state/)
MQTT_CA_CERT=/etc/sbnb/mqtt/ca.crt
MQTT_CLIENT_CERT=/etc/sbnb/mqtt/bootstrap.crt
MQTT_CLIENT_KEY=/etc/sbnb/mqtt/bootstrap.key

# Topic prefix (optional, defaults to sbnb)
# MQTT_TOPIC_PREFIX=sbnb
```

## Certificate Management

### Bootstrap Certificates (Shared)

- Used during initial device registration
- Placed on USB flash drive (read-only)
- Same certificate can be used for multiple devices
- Low privileges - can only register new devices

### Device Certificates (Individual)

- Assigned after successful registration
- Stored in `/mnt/sbnb-data/state/` (persistent storage)
- Unique per device, identified by UUID
- Full privileges for that specific device

### Certificate Hierarchy

```
Root CA
└── Intermediate CA (signs both types below)
    ├── Bootstrap Certificate (shared, read-only from USB)
    └── Device Certificates (individual, stored in persistent data)
```

## Service Behavior

The `sbnb-mqtt.service` systemd unit:

- **Starts automatically** if CA certificate exists at `/etc/sbnb/mqtt/ca.crt`
- **Auto-restarts** on failure (network issues, broker downtime)
- **Resource limited** - max 256MB RAM, 10% CPU quota
- **Event-driven** - no polling, uses MQTT pub/sub with retain flags

Check service status:
```bash
systemctl status sbnb-mqtt
journalctl -u sbnb-mqtt -f
```

## MQTT Topics

### Device → Broker

- `sbnb/devices/bootstrap` - Initial registration (retain flag, QoS 1)
- `sbnb/devices/{uuid}/status` - Periodic status updates
- `sbnb/devices/{uuid}/logs` - Log messages (optional)

### Broker → Device

- `sbnb/devices/{uuid}/config` - Configuration updates (retain flag, QoS 1)
- `sbnb/devices/{uuid}/commands` - One-time commands (QoS 1)
- `sbnb/devices/{uuid}/provision` - Provisioning response with certificates

## Message Formats

### Registration Message

```json
{
  "mac": "00:11:22:33:44:55",
  "hostname": "sbnb-0011223344",
  "image_version": "1.0.0",
  "timestamp": "2026-03-06T12:00:00Z"
}
```

### Provisioning Response

```json
{
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "device_cert": "-----BEGIN CERTIFICATE-----\n...",
  "device_key": "-----BEGIN PRIVATE KEY-----\n...",
  "mqtt_broker": "mqtt.example.com",
  "mqtt_port": 8883
}
```

### Configuration Bundle

```json
{
  "config_url": "https://console.example.com/configs/device-uuid.tar.gz",
  "checksum": "sha256:abcd1234...",
  "timestamp": "2026-03-06T12:00:00Z"
}
```

## Troubleshooting

### Service Won't Start

Check if CA certificate exists:
```bash
ls -l /etc/sbnb/mqtt/ca.crt
```

View service conditions:
```bash
systemctl show sbnb-mqtt | grep Condition
```

### Connection Issues

Verify certificates:
```bash
openssl verify -CAfile /etc/sbnb/mqtt/ca.crt /etc/sbnb/mqtt/bootstrap.crt
```

Test broker connectivity:
```bash
mosquitto_pub -h mqtt.example.com -p 8883 \
  --cafile /etc/sbnb/mqtt/ca.crt \
  --cert /etc/sbnb/mqtt/bootstrap.crt \
  --key /etc/sbnb/mqtt/bootstrap.key \
  -t test -m "hello"
```

### View Logs

```bash
# Follow live logs
journalctl -u sbnb-mqtt -f

# Show recent logs
journalctl -u sbnb-mqtt -n 100

# Logs since last boot
journalctl -u sbnb-mqtt -b
```

## Security Considerations

1. **Bootstrap certificate is shared** - treat it as sensitive but not critical
   - Can only register new devices, cannot control existing ones
   - Rotate periodically if compromised

2. **Device certificates are unique** - each device gets its own
   - Revoke compromised device certs without affecting others
   - Stored in persistent storage, not on read-only USB

3. **CA certificate verifies broker identity** - prevents man-in-the-middle attacks
   - Must match the certificate presented by your MQTT broker

4. **mTLS required** - broker validates client certificates
   - No username/password authentication needed
   - Certificate-based identity is more secure

## Hybrid Mode (Tailscale + MQTT)

Both connectivity methods can coexist:

- **Tailscale** - for SSH access, interactive management, real-time debugging
- **MQTT** - for automated config distribution, event-driven updates

Place both on USB flash:
```
/mnt/sbnb/
├── tailscale/
│   └── tskey-*.txt
└── mqtt/
    ├── mqtt.conf
    ├── ca.crt
    ├── bootstrap.crt
    └── bootstrap.key
```

Both services will start automatically and operate independently.

## Example: Self-Hosted EMQX Broker

SBNB provides automated setup scripts in `tools/mqtt/`. Quick setup:

```bash
# Generate certificates and start EMQX broker
cd tools/mqtt
./setup-mqtt-server.sh -d mqtt.example.com -o ./mqtt-prod -s

# Access web dashboard
open http://localhost:18083  # Default: admin/public
```

The setup script automatically configures:
- **mTLS authentication** - Client certificate verification
- **ACL rules** - Topic-based access control
- **Multi-tenancy** - Namespace isolation
- **Dashboard** - Real-time monitoring and management

Manual EMQX ACL configuration (`acl.conf`):
```erlang
%% Bootstrap certificate can only publish to registration topic
%% (peer_cert_as_username=cn extracts just "bootstrap" from CN=bootstrap)
{allow, {user, "bootstrap"}, publish, ["sbnb/devices/bootstrap"]}.

%% Any authenticated user can access their own topics
{allow, all, all, ["sbnb/devices/${username}/#"]}.

%% Deny all other access
{deny, all}.
```

For detailed broker setup, see [tools/mqtt/README.md](../tools/mqtt/README.md)

## Disabling MQTT

To disable MQTT support:

1. Remove the `mqtt/` directory from USB flash
2. Service won't start (condition check fails)
3. Or explicitly disable: `systemctl disable sbnb-mqtt`

No need to rebuild the image - MQTT support is always present but conditionally activated.

## EMQX Broker

SBNB uses EMQX as the recommended MQTT broker for its enterprise features:

- **Web Dashboard** - Real-time monitoring at http://localhost:18083 (default: admin/public)
- **Multi-tenancy** - Native support for isolating different deployments
- **Scalability** - Handles millions of concurrent connections
- **Rule Engine** - Process messages with SQL-like rules
- **Metrics** - Built-in Prometheus integration
- **MQTT 5.0** - Latest protocol features

See [tools/mqtt/README.md](../tools/mqtt/README.md) for complete broker setup instructions.

## See Also

- [MQTT-ARCHITECTURE.md](./MQTT-ARCHITECTURE.md) - Detailed architecture and design decisions
- [tools/mqtt/README.md](../tools/mqtt/README.md) - Broker setup and management tools
- [Paho MQTT Client](https://pypi.org/project/paho-mqtt/) - Python library documentation
- [EMQX Documentation](https://www.emqx.io/docs/en/latest/) - EMQX broker documentation
- [MQTT Protocol](https://mqtt.org/) - MQTT specification and resources
