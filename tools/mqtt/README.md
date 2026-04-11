# SBNB MQTT Server Tools

This directory contains tools for setting up and managing the SBNB MQTT+mTLS infrastructure using **EMQX** broker.

## Overview

These scripts help you:
1. **Generate certificates** - CA, broker, bootstrap, and device-specific certs
2. **Configure EMQX broker** - Generate config with mTLS authentication and multi-tenancy
3. **Start MQTT broker** - Docker-based EMQX deployment with web dashboard
4. **Prepare USB bundles** - Create portable MQTT configuration for devices
5. **Inject into images** - Add MQTT config directly to raw disk images

## Why EMQX?

- **Native multi-tenancy** - Isolate different customers/deployments
- **Web dashboard** - Real-time monitoring and management (http://localhost:18083)
- **Enterprise features** - Clustering, metrics, rule engine
- **Better scalability** - Handles millions of concurrent connections
- **MQTT 5.0 support** - Latest MQTT protocol features

## Quick Start

### 1. Initial Setup

Generate certificates, configure broker, and create USB bundle:

```bash
./setup-mqtt-server.sh -d mqtt.mycompany.com -o ./mqtt-server
```

This creates:
- `mqtt-server/certs/` - CA, broker, and bootstrap certificates
- `mqtt-server/broker-config/` - EMQX configuration and docker-compose.yml
- `mqtt-server/usb-bundle/mqtt/` - USB flash bundle ready to copy to devices
- `mqtt-server/device-certs/` - Directory for device-specific certificates (generated on-demand)

### 2. Start EMQX Broker

Start EMQX in Docker:

```bash
cd mqtt-server/broker-config
docker compose up -d

# View logs
docker compose logs -f

# Access web dashboard
open http://localhost:18083  # Default: admin/public
```

Or use the script:

```bash
./setup-mqtt-server.sh --skip-certs -s -o ./mqtt-server
```

The EMQX dashboard provides:
- Real-time client connections and message flow
- Topic subscription monitoring
- ACL rule testing
- Prometheus metrics
- WebSocket client for testing

### 3. Prepare Device

**Option A: Copy to USB flash**

```bash
# Mount your USB drive
sudo mount /dev/sdb1 /mnt/usb

# Copy MQTT configuration
sudo cp -r mqtt-server/usb-bundle/mqtt /mnt/usb/

# Unmount
sudo umount /mnt/usb
```

**Option B: Inject into raw image**

```bash
sudo ./inject-mqtt-config.sh -i ../../buildroot/output/images/sbnb.raw
```

### 4. Boot Device

Boot your SBNB device with the USB flash drive or injected image:

```bash
# Test locally with QEMU
../../scripts/sbnb-local-boot.sh
```

The device will:
1. Copy MQTT config from USB/image to `/etc/sbnb/mqtt/`
2. Start `sbnb-mqtt` service
3. Register with broker using bootstrap certificate
4. Wait for provisioning response with device-specific certificate

### 5. Monitor Broker

View device registrations via EMQX dashboard or CLI:

```bash
# Option 1: Use EMQX web dashboard
open http://localhost:18083  # admin/public

# Option 2: Use mosquitto_sub CLI
mosquitto_sub -h localhost -p 8883 \
  --cafile mqtt-server/certs/ca.crt \
  --cert mqtt-server/certs/bootstrap.crt \
  --key mqtt-server/certs/bootstrap.key \
  -t 'sbnb/devices/bootstrap' -v
```

## Scripts

### setup-mqtt-server.sh

Main setup script that generates certificates, configures EMQX broker, and creates USB bundle.

**Usage:**
```bash
./setup-mqtt-server.sh [OPTIONS]
```

**Options:**
- `-d, --domain DOMAIN` - MQTT broker domain or IP (default: mqtt.example.com)
- `-p, --port PORT` - MQTT broker port (default: 8883)
- `-o, --output DIR` - Output directory (default: ./mqtt-server)
- `-s, --start-broker` - Start EMQX broker in Docker
- `--skip-certs` - Skip certificate generation (use existing)

**Examples:**

```bash
# Generate everything with custom domain
./setup-mqtt-server.sh -d mqtt.example.com -o ./mqtt-server

# Use IP address instead of domain
./setup-mqtt-server.sh -d 192.168.40.42 -o ./mqtt-server

# Start broker with existing certs
./setup-mqtt-server.sh --skip-certs -s -o ./mqtt-server
```

### inject-mqtt-config.sh

Injects MQTT configuration into a raw SBNB disk image. Run this after every image rebuild.
Uses the same `-o` output directory as `setup-mqtt-server.sh` (defaults to `./mqtt-server`).

**Usage:**
```bash
sudo ./inject-mqtt-config.sh [OPTIONS]
```

**Options:**
- `-o, --output DIR` - Output directory from setup-mqtt-server.sh (default: ./mqtt-server)
- `-i, --image FILE` - Raw disk image to inject into (required)

**Examples:**

```bash
# Inject using default output directory (./mqtt-server)
sudo ./inject-mqtt-config.sh -i ../../buildroot/output/images/sbnb.raw

# Inject using custom output directory
sudo ./inject-mqtt-config.sh -o ./mqtt-prod -i /path/to/sbnb.raw
```

### generate-device-cert.sh

Generates device-specific certificates for provisioned devices. The console would use this when a device registers.

**Usage:**
```bash
./generate-device-cert.sh -u UUID -c CA_DIR [-o OUTPUT_DIR] [-j]
```

**Options:**
- `-u, --uuid UUID` - Device UUID (required)
- `-c, --ca-dir DIR` - Directory containing CA cert and key (required)
- `-o, --output DIR` - Output directory (default: ./device-certs/UUID)
- `-j, --json` - Output certificate as JSON (for MQTT provisioning message)

**Examples:**

```bash
# Generate certificate for device
./generate-device-cert.sh -u 550e8400-e29b-41d4-a716-446655440000 -c ./mqtt-server/certs

# Generate and output as JSON for MQTT message
./generate-device-cert.sh -u abc-123 -c ./mqtt-server/certs -j > provision-message.json
```

## Directory Structure

After running `setup-mqtt-server.sh -o mqtt-server`:

```
mqtt-server/
├── certs/
│   ├── ca.crt               # CA certificate (for verification)
│   ├── ca.key               # CA private key (keep secure!)
│   ├── broker.crt           # Broker server certificate
│   ├── broker.key           # Broker private key
│   ├── bootstrap.crt        # Shared bootstrap cert (for device registration)
│   └── bootstrap.key        # Bootstrap private key
├── broker-config/
│   ├── acl.conf             # Access control list (EMQX format)
│   ├── emqx.env             # EMQX environment configuration
│   └── docker-compose.yml   # Docker Compose file for EMQX broker
├── usb-bundle/
│   └── mqtt/
│       ├── mqtt.conf        # MQTT connection settings
│       ├── ca.crt           # CA certificate (copy of above)
│       ├── bootstrap.crt    # Bootstrap cert (copy of above)
│       ├── bootstrap.key    # Bootstrap key (copy of above)
│       └── README.txt       # Usage instructions
└── device-certs/
    └── {uuid}/
        ├── device.crt       # Device-specific certificate
        └── device.key       # Device-specific private key
```

## Workflow

### Device Registration Flow

1. **Device boots** with USB containing bootstrap certificates
2. **Device registers** by publishing to `sbnb/devices/bootstrap` with MAC and hostname
3. **Console receives** registration via MQTT subscription
4. **Console generates** device-specific certificate:
   ```bash
   ./generate-device-cert.sh -u NEW_UUID -c ./mqtt-server/certs -j
   ```
5. **Console sends** provisioning response to `sbnb/devices/{uuid}/provision` with device cert
6. **Device receives** cert, stores in `/mnt/sbnb-data/state/`, reconnects with device cert
7. **Device subscribes** to `sbnb/devices/{uuid}/config` for configuration updates

### Configuration Distribution Flow

1. **Console publishes** config to `sbnb/devices/{uuid}/config` (with retain flag)
2. **Device receives** config message with download URL and checksum
3. **Device downloads** configuration bundle (tar.gz)
4. **Device verifies** checksum
5. **Device extracts** and applies configuration
6. **Device sends** status update to `sbnb/devices/{uuid}/status`

## Testing

### Test Broker Connection

**Option 1: Using EMQX Web Dashboard**

Open http://localhost:18083 (admin/public) and use the built-in WebSocket client to:
- Subscribe to topics
- Publish test messages
- View real-time message flow
- Test ACL rules

**Option 2: Using mosquitto_pub/sub CLI**

```bash
cd mqtt-server

# Subscribe to bootstrap topic
mosquitto_sub -h localhost -p 8883 \
  --cafile certs/ca.crt \
  --cert certs/bootstrap.crt \
  --key certs/bootstrap.key \
  -t 'sbnb/devices/bootstrap' -v

# Publish test message (in another terminal)
mosquitto_pub -h localhost -p 8883 \
  --cafile certs/ca.crt \
  --cert certs/bootstrap.crt \
  --key certs/bootstrap.key \
  -t 'sbnb/devices/bootstrap' \
  -m '{"mac":"00:11:22:33:44:55","hostname":"test-device"}'
```

**Option 3: Run test script**

```bash
./test-mqtt-broker.sh -c mqtt-server/certs
```

### Test Device Certificate

After generating a device cert:

```bash
UUID="550e8400-e29b-41d4-a716-446655440000"

# Subscribe to device config topic
mosquitto_sub -h localhost -p 8883 \
  --cafile mqtt-server/certs/ca.crt \
  --cert mqtt-server/device-certs/${UUID}/device.crt \
  --key mqtt-server/device-certs/${UUID}/device.key \
  -t "sbnb/devices/${UUID}/config" -v
```

## Security Considerations

### Certificate Management

1. **CA Private Key** (`ca.key`)
   - Most sensitive - can sign any certificate
   - Store securely, use hardware security module (HSM) in production
   - Backup and encrypt

2. **Bootstrap Certificate** (`bootstrap.crt/key`)
   - Shared across devices for initial registration
   - Limited permissions (can only write to bootstrap topic)
   - Rotate periodically (every 6-12 months)
   - If compromised: revoke and regenerate, update all USB bundles

3. **Device Certificates** (per-device `device.crt/key`)
   - Unique per device, identified by UUID
   - Full access to device-specific topics
   - If compromised: revoke single device cert without affecting others
   - Store securely in persistent storage

4. **Broker Certificate** (`broker.crt/key`)
   - Server certificate for broker authentication
   - Must match domain in DNS
   - Rotate before expiration (generated with 10-year validity)

### Access Control

The ACL (`acl.conf`) enforces topic-based permissions:

```
# Bootstrap can only register
user CN=bootstrap
topic write sbnb/devices/bootstrap

# Devices can only access their own topics (UUID must match CN)
pattern readwrite sbnb/devices/%u/#
```

### Network Security

- Use firewall rules to restrict broker access
- Consider VPN or private network for broker-console communication
- Monitor for suspicious connection attempts
- Rate limit connections to prevent DoS

## Troubleshooting

### Broker Won't Start

Check logs:
```bash
cd mqtt-server/broker-config
docker compose logs
```

Common issues:
- Port 8883 already in use: `sudo netstat -tlnp | grep 8883`
- Certificate permissions: ensure certs are readable by EMQX container
- Invalid certificate paths in emqx.env

### Device Can't Connect

Check device logs:
```bash
# On SBNB device
journalctl -u sbnb-mqtt -f
```

Common issues:
- CA certificate mismatch (broker cert not signed by provided CA)
- Bootstrap certificate expired or invalid
- Broker domain doesn't match certificate CN
- Network connectivity (firewall, DNS resolution)

### Certificate Verification Failed

Verify certificate chain:
```bash
# Verify broker cert is signed by CA
openssl verify -CAfile mqtt-server/certs/ca.crt mqtt-server/certs/broker.crt

# Verify bootstrap cert is signed by CA
openssl verify -CAfile mqtt-server/certs/ca.crt mqtt-server/certs/bootstrap.crt

# Check certificate details
openssl x509 -in mqtt-server/certs/broker.crt -noout -text
```

### Test Connection with OpenSSL

```bash
# Test TLS handshake
openssl s_client -connect localhost:8883 \
  -CAfile mqtt-server/certs/ca.crt \
  -cert mqtt-server/certs/bootstrap.crt \
  -key mqtt-server/certs/bootstrap.key
```

## Advanced Topics

### Certificate Rotation

Rotate bootstrap certificate:
```bash
# Generate new bootstrap cert
cd mqtt-server/certs
openssl genrsa -out bootstrap-new.key 2048
openssl req -new -key bootstrap-new.key -out bootstrap-new.csr \
  -subj "/C=US/ST=FL/L=Miami/O=SBNB/OU=Devices/CN=bootstrap"
openssl x509 -req -in bootstrap-new.csr -CA ca.crt -CAkey ca.key \
  -CAcreateserial -out bootstrap-new.crt -days 3650

# Test new cert
# Then replace old cert
mv bootstrap.crt bootstrap-old.crt
mv bootstrap.key bootstrap-old.key
mv bootstrap-new.crt bootstrap.crt
mv bootstrap-new.key bootstrap.key

# Update USB bundles and restart broker
```

### Production Deployment

For production use:

1. **Use proper PKI infrastructure** - consider commercial CA or internal PKI
2. **Separate CA systems** - different CAs for bootstrap and device certs
3. **Certificate revocation** - implement CRL or OCSP
4. **High availability** - run multiple broker instances with load balancer
5. **Monitoring** - track connection attempts, message rates, errors
6. **Backup** - regular backups of CA keys and broker data
7. **Automation** - integrate with your device provisioning system

### Multi-Tenant Setup

Use topic prefixes for isolation:

```bash
# Generate separate configs for different customers
./setup-mqtt-server.sh -d mqtt.customer1.com -o ./customer1
./setup-mqtt-server.sh -d mqtt.customer2.com -o ./customer2
```

Update `mqtt.conf` with different topic prefixes:
```ini
MQTT_TOPIC_PREFIX=customer1
```

## See Also

- [MQTT-ARCHITECTURE.md](../../docs/MQTT-ARCHITECTURE.md) - Detailed architecture documentation
- [README-MQTT.md](../../docs/README-MQTT.md) - Device-side MQTT configuration
- [EMQX-MIGRATION.md](./EMQX-MIGRATION.md) - Migration notes from Mosquitto to EMQX
- [EMQX Documentation](https://www.emqx.io/docs/en/latest/)
- [MQTT v3.1.1 Specification](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/mqtt-v3.1.1.html)
