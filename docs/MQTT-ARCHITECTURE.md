# MQTT+mTLS Architecture Plan

**Status:** Design Phase
**Date:** 2026-03-06
**Version:** 1.0

## Executive Summary

This document describes the architecture for adding MQTT+mTLS as an optional communication channel alongside Tailscale in SBNB. The goal is to provide efficient pull-based configuration management while maintaining backward compatibility with existing Tailscale deployments.

**Key Principles:**
- ✅ Non-breaking: Existing Tailscale deployments continue to work
- ✅ Additive: MQTT is a parallel option, not a replacement
- ✅ Open source friendly: Base image contains no vendor-specific credentials
- ✅ Simple UX: Users download pre-configured image from console.sbnb.io
- ✅ Self-hostable: Users can run their own MQTT broker

---

## Architecture Overview

### Communication Channels

```
┌──────────────────────────────────────────┐
│         Bare Metal Host                   │
├──────────────────────────────────────────┤
│                                           │
│  [Tailscale] ←→ SSH access (optional)    │
│      ↓                                    │
│   Push-mode: ansible-playbook            │
│                                           │
│  [MQTT Client] ←→ Pull-mode (new)        │
│      ↓                                    │
│   Subscribe: devices/{uuid}/commands     │
│   Publish: devices/{uuid}/status         │
│                                           │
└──────────────────────────────────────────┘
          ↓                    ↓
    (push-mode)          (pull-mode)
          ↓                    ↓
┌──────────────────────────────────────────┐
│       console.sbnb.io                     │
├──────────────────────────────────────────┤
│  SSH via Tailscale (optional)            │
│  MQTT Broker (mTLS)                      │
│  Web Dashboard                            │
└──────────────────────────────────────────┘
```

### Deployment Matrix

| USB Contents | Tailscale | MQTT | Reconciler | Use Case |
|--------------|-----------|------|------------|----------|
| Nothing (base image) | ❌ | ❌ | None | Build/development |
| Tailscale key only | ✅ | ❌ | REST polling | Legacy (backward compat) |
| MQTT certs only | ❌ | ✅ | MQTT pub/sub | Pure MQTT |
| Both | ✅ | ✅ | MQTT pub/sub | **Hybrid (recommended)** |

---

## User Journey

### 1. User Registration & Download

```
1. User registers at console.sbnb.io
   ↓
2. Console generates customer-specific bootstrap certificate
   ↓
3. User clicks "Download Image"
   ↓
4. Console server:
   - Mounts base sbnb.raw (no credentials)
   - Injects bootstrap cert to /etc/sbnb/mqtt/
   - Optionally configures MQTT broker URL
   - Serves modified image
   ↓
5. User receives sbnb.raw with embedded credentials
```

### 2. Device Boot & Registration

```
1. User flashes USB: dd if=sbnb.raw of=/dev/sdX
   ↓
2. User boots metal host from USB
   ↓
3. Device boots, generates hostname: sbnb-{MAC}
   ↓
4. Device reads embedded bootstrap certificate
   ↓
5. Device connects to MQTT broker (console.sbnb.io:8883)
   ↓
6. Device generates RSA-2048 keypair + CSR (private key stays on device)
   ↓
7. Device publishes to: devices/bootstrap/{hostname}/register
   Payload: {"hostname": "sbnb-525400123456", "mac": "...", "csr": "-----BEGIN CERTIFICATE REQUEST-----..."}
   ↓
8. Admin approves device (signs CSR, assigns UUID)
   ↓
9. Device receives signed certificate (no private key transmitted)
   ↓
10. Device appears in dashboard: "New Device - sbnb-525400123456"
```

### 3. User Configuration

```
1. User sees device in dashboard
   Status: ⚠️  Awaiting Configuration
   ↓
2. User clicks "Configure Device"
   ↓
3. Web UI shows:
   - Device naming
   - Application catalog (Docker, Ollama, OpenClaw, Frigate, etc.)
   - VM configuration
   ↓
4. User selects apps: [✓] Docker, [✓] OpenClaw, [✓] Ollama
   ↓
5. User clicks "Save & Provision"
```

### 4. Automated Provisioning

```
1. Console creates customer bundle:
   - inventory.yml
   - vars.yml (selected apps)
   - provision.yml (playbook)
   ↓
2. Admin/Console publishes to: devices/bootstrap/{hostname}/provision
   Payload: {
     "uuid": "550e8400-...",
     "device_cert": "-----BEGIN CERTIFICATE-----...",
     "bundle_url": "https://console.sbnb.io/api/bundles/customer"
   }
   Note: No private key transmitted — device generated it locally via CSR
   ↓
3. Device receives signed certificate
   ↓
4. Device saves UUID + certificate to /mnt/sbnb-data/state/
   (private key already saved during CSR generation)
   ↓
5. Device downloads customer bundle
   ↓
6. Device runs provision playbook
   ↓
7. Device publishes status updates:
   - "provisioning" → "Installing Docker" → "online"
   ↓
8. Device appears in dashboard: 🟢 Online
```

---

## Technical Architecture

### 1. Image Build (Open Source)

**No credentials in base image:**

```
board/sbnb/sbnb/rootfs-overlay/
└── etc/
    └── sbnb/
        ├── mqtt.conf.example          # Template configuration
        └── mqtt/                       # Empty (credentials injected at download)
```

**mqtt.conf.example:**
```ini
# MQTT Broker Configuration
MQTT_BROKER=console.sbnb.io
MQTT_PORT=8883

# Certificate paths
MQTT_CA_CERT=/etc/sbnb/mqtt/ca.crt
MQTT_CLIENT_CERT=/etc/sbnb/mqtt/client.crt
MQTT_CLIENT_KEY=/etc/sbnb/mqtt/client.key

# Device certificate (after provisioning)
MQTT_DEVICE_CERT=/mnt/sbnb-data/state/device.crt
MQTT_DEVICE_KEY=/mnt/sbnb-data/state/device.key
```

**Buildroot configuration:**
```
# configs/sbnb_defconfig
BR2_PACKAGE_PYTHON_PAHO_MQTT=y
```

### 2. Download-Time Credential Injection

**Console server workflow:**

```python
@app.route('/download/sbnb.raw')
def download_image(user_id):
    # 1. Mount base image (read-only)
    mount_image('base-sbnb.raw', '/mnt/base', readonly=True)

    # 2. Create working copy
    copy('base-sbnb.raw', f'/tmp/sbnb-{user_id}.raw')

    # 3. Mount working copy (read-write)
    mount_image(f'/tmp/sbnb-{user_id}.raw', '/mnt/work', readonly=False)

    # 4. Get customer credentials
    certs = get_customer_bootstrap_certs(user_id)

    # 5. Inject certificates
    inject_file('/mnt/work/etc/sbnb/mqtt/ca.crt', certs['ca'])
    inject_file('/mnt/work/etc/sbnb/mqtt/bootstrap.crt', certs['bootstrap_cert'])
    inject_file('/mnt/work/etc/sbnb/mqtt/bootstrap.key', certs['bootstrap_key'])

    # 6. Optionally inject custom broker URL
    if customer.has_custom_broker:
        config = f"MQTT_BROKER={customer.mqtt_broker}\nMQTT_PORT={customer.mqtt_port}"
        inject_file('/mnt/work/etc/sbnb/mqtt.conf', config)

    # 7. Unmount and serve
    unmount('/mnt/work')
    return send_file(f'/tmp/sbnb-{user_id}.raw')
```

**Injected structure:**
```
/etc/sbnb/mqtt/
├── ca.crt              # Broker CA certificate
├── bootstrap.crt       # Shared bootstrap cert (customer-specific or global)
└── bootstrap.key       # Shared bootstrap key
```

### 3. Boot Sequence

**Modified boot-sbnb.sh:**

```bash
#!/bin/sh

# ... existing functions ...

start_mqtt() {
    # Read MQTT configuration
    MQTT_CONF="/etc/sbnb/mqtt.conf"
    if [ -f "/mnt/sbnb-data/state/mqtt.conf" ]; then
        # Persistent override takes precedence
        source "/mnt/sbnb-data/state/mqtt.conf"
        echo "[sbnb] Using persistent MQTT config"
    elif [ -f "${MQTT_CONF}" ]; then
        # Injected config
        source "${MQTT_CONF}"
        echo "[sbnb] Using injected MQTT config"
    else
        # Defaults
        MQTT_BROKER="console.sbnb.io"
        MQTT_PORT="8883"
        echo "[sbnb] Using default MQTT config"
    fi

    echo "[sbnb] MQTT broker: ${MQTT_BROKER}:${MQTT_PORT}"

    # Determine which certificate to use
    if [ -f "/mnt/sbnb-data/state/device.crt" ] && [ -f "/mnt/sbnb-data/state/device.key" ]; then
        # Device-specific certificate (already provisioned)
        echo "[sbnb] Using device-specific certificate"
        export MQTT_CLIENT_CERT="/mnt/sbnb-data/state/device.crt"
        export MQTT_CLIENT_KEY="/mnt/sbnb-data/state/device.key"
    elif [ -f "/etc/sbnb/mqtt/bootstrap.crt" ] && [ -f "/etc/sbnb/mqtt/bootstrap.key" ]; then
        # Bootstrap certificate (first boot or re-provisioning)
        echo "[sbnb] Using bootstrap certificate"
        export MQTT_CLIENT_CERT="/etc/sbnb/mqtt/bootstrap.crt"
        export MQTT_CLIENT_KEY="/etc/sbnb/mqtt/bootstrap.key"
    else
        echo "[sbnb] No MQTT certificates found, skipping MQTT"
        return 0
    fi

    export MQTT_CA_CERT="/etc/sbnb/mqtt/ca.crt"
    export MQTT_BROKER
    export MQTT_PORT

    # Enable and start MQTT service
    systemctl enable sbnb-mqtt.service
    systemctl start sbnb-mqtt.service
}

# Main execution
set_hostname
mount_sbnb_usb
mount_vmware_shared_folder
execute_sbnb_cmds
start_tunnel      # Tailscale (if tunnel-start.sh exists)
start_mqtt        # MQTT (if certificates exist)
display_banner
```

### 4. MQTT Topic Structure

```
devices/
├── bootstrap/
│   └── {hostname}/                       # Per-hostname topics (retained)
│       ├── register                      # Device publishes registration + CSR
│       │   └── Payload: {
│       │         "hostname": "sbnb-{MAC}",
│       │         "mac": "...",
│       │         "timestamp": ...,
│       │         "csr": "-----BEGIN CERTIFICATE REQUEST-----..."
│       │       }
│       │
│       └── provision                     # Admin publishes signed cert (no private key)
│           └── Payload: {
│                 "uuid": "550e8400-...",
│                 "device_cert": "-----BEGIN CERTIFICATE-----...",
│                 "bundle_url": "https://..." (optional)
│               }
│
└── {uuid}/                               # After provisioning (device-specific cert)
    ├── commands                          # Console → Device
    │   └── Examples:
    │       - {"action": "apply_config", "bundle_url": "...", "version": "1.2.3"}
    │       - {"action": "reboot"}
    │       - {"action": "update_collection", "version": "..."}
    │
    ├── status                            # Device → Console
    │   └── Examples:
    │       - {"state": "online", "hostname": "...", "apps": [...]}
    │       - {"state": "provisioning", "step": "Installing Docker", "progress": 30}
    │       - {"state": "error", "message": "..."}
    │
    └── metrics                           # Device → Console (optional)
        └── Payload: {"cpu": 45, "mem": 70, "disk": 60, "vms": [...]}
```

### 5. MQTT Reconciler Service

**Systemd service:**

```ini
# /usr/lib/systemd/system/sbnb-mqtt.service

[Unit]
Description=SBNB MQTT Reconciler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=-/etc/sbnb/mqtt.conf
ExecStart=/usr/bin/sbnb-mqtt-reconciler
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Reconciler implementation:**

```python
#!/usr/bin/env python3
"""
SBNB MQTT Reconciler - Pull-based configuration management via MQTT
"""

import json
import os
import subprocess
import time
import paho.mqtt.client as mqtt
import ssl

class MQTTReconciler:
    def __init__(self):
        # Read configuration
        self.broker = os.getenv('MQTT_BROKER', 'console.sbnb.io')
        self.port = int(os.getenv('MQTT_PORT', '8883'))
        self.hostname = os.uname().nodename  # sbnb-{MAC}

        # Determine certificate type
        device_cert = '/mnt/sbnb-data/state/device.crt'
        bootstrap_cert = '/etc/sbnb/mqtt/bootstrap.crt'

        if os.path.exists(device_cert):
            self.mode = 'device'
            self.device_uuid = self._read_uuid()
            self.client_cert = device_cert
            self.client_key = '/mnt/sbnb-data/state/device.key'
            print(f"[mqtt] Device mode: UUID={self.device_uuid}")
        elif os.path.exists(bootstrap_cert):
            self.mode = 'bootstrap'
            self.client_cert = bootstrap_cert
            self.client_key = '/etc/sbnb/mqtt/bootstrap.key'
            print(f"[mqtt] Bootstrap mode: hostname={self.hostname}")
        else:
            raise FileNotFoundError("No MQTT certificates found")

        self.ca_cert = '/etc/sbnb/mqtt/ca.crt'
        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect

    def _read_uuid(self):
        with open('/mnt/sbnb-data/state/device-uuid', 'r') as f:
            return f.read().strip()

    def _get_mac(self):
        with open('/sys/class/net/eth0/address', 'r') as f:
            return f.read().strip()

    def on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            print(f"[mqtt] Connection failed: rc={rc}")
            return

        print(f"[mqtt] Connected to {self.broker}:{self.port}")

        if self.mode == 'bootstrap':
            self._handle_bootstrap_connect(client)
        else:
            self._handle_device_connect(client)

    def on_disconnect(self, client, userdata, rc):
        print(f"[mqtt] Disconnected: rc={rc}")

    def _handle_bootstrap_connect(self, client):
        """Bootstrap mode: Register and wait for provisioning"""
        # Subscribe to provisioning response
        topic = f"devices/bootstrap/{self.hostname}/provision"
        client.subscribe(topic)
        print(f"[mqtt] Subscribed to {topic}")

        # Publish registration
        def register():
            payload = json.dumps({
                "hostname": self.hostname,
                "mac": self._get_mac(),
                "timestamp": time.time()
            })
            client.publish("devices/bootstrap/register", payload, retain=True)
            print(f"[mqtt] Published registration")

        # Register immediately and every 30s
        register()
        self._registration_timer = client.loop_start()
        while self.mode == 'bootstrap':
            time.sleep(30)
            register()

    def _handle_device_connect(self, client):
        """Device mode: Subscribe to commands"""
        topic = f"devices/{self.device_uuid}/commands"
        client.subscribe(topic)
        print(f"[mqtt] Subscribed to {topic}")

        # Publish online status
        status_topic = f"devices/{self.device_uuid}/status"
        status = json.dumps({
            "state": "online",
            "hostname": self.hostname,
            "mac": self._get_mac(),
            "timestamp": time.time()
        })
        client.publish(status_topic, status)

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())

            if self.mode == 'bootstrap':
                self._handle_bootstrap_message(payload)
            else:
                self._handle_device_message(payload)
        except Exception as e:
            print(f"[mqtt] Error handling message: {e}")

    def _handle_bootstrap_message(self, payload):
        """Handle provisioning response"""
        uuid = payload['uuid']
        certificate = payload['certificate']
        private_key = payload['private_key']
        bundle_url = payload.get('bundle_url')

        print(f"[mqtt] Received provisioning: UUID={uuid}")

        # Save to /tmp (will be moved by bootstrap playbook)
        os.makedirs('/tmp/sbnb-provision', exist_ok=True)

        with open('/tmp/sbnb-provision/device-uuid', 'w') as f:
            f.write(uuid)
        with open('/tmp/sbnb-provision/device.crt', 'w') as f:
            f.write(certificate)
        with open('/tmp/sbnb-provision/device.key', 'w') as f:
            f.write(private_key)

        # Download and run bootstrap bundle
        if bundle_url:
            print(f"[mqtt] Downloading bootstrap bundle from {bundle_url}")
            subprocess.run([
                'curl', '-f', '-o', '/tmp/bootstrap.tar.gz',
                '-H', f'X-Device-UUID: {uuid}',
                bundle_url
            ], check=True)

            # Extract and run
            os.makedirs('/tmp/bootstrap', exist_ok=True)
            subprocess.run(['tar', 'xzf', '/tmp/bootstrap.tar.gz', '-C', '/tmp/bootstrap'], check=True)

            print(f"[mqtt] Running bootstrap playbook")
            result = subprocess.run(
                ['ansible-playbook', 'bootstrap.yml'],
                cwd='/tmp/bootstrap'
            )

            if result.returncode == 0:
                print("[mqtt] Bootstrap complete, restarting with device cert")
                subprocess.run(['systemctl', 'restart', 'sbnb-mqtt.service'])
            else:
                print(f"[mqtt] Bootstrap failed: {result.returncode}")

    def _handle_device_message(self, payload):
        """Handle device commands"""
        action = payload.get('action')

        if action == 'apply_config':
            self._apply_config(payload)
        elif action == 'reboot':
            self._reboot()
        elif action == 'update_collection':
            self._update_collection(payload)
        else:
            print(f"[mqtt] Unknown action: {action}")

    def _apply_config(self, payload):
        """Download and apply customer configuration"""
        bundle_url = payload['bundle_url']
        version = payload['version']

        print(f"[mqtt] Applying config version {version}")
        self._publish_status('applying', f'Downloading bundle {version}')

        # Download
        bundle_path = f'/mnt/sbnb-data/cache/customer-{version}.tar.gz'
        os.makedirs('/mnt/sbnb-data/cache', exist_ok=True)

        subprocess.run([
            'curl', '-f', '-o', bundle_path,
            '-H', f'X-Device-UUID: {self.device_uuid}',
            bundle_url
        ], check=True)

        # Extract
        extract_dir = f'/mnt/sbnb-data/cache/customer-{version}'
        os.makedirs(extract_dir, exist_ok=True)
        subprocess.run(['tar', 'xzf', bundle_path, '-C', extract_dir], check=True)

        # Run playbook
        self._publish_status('applying', f'Running playbook {version}')

        result = subprocess.run(
            ['ansible-playbook', '-i', 'inventory.yml', 'provision.yml'],
            cwd=extract_dir,
            capture_output=True
        )

        if result.returncode == 0:
            self._publish_status('applied', f'Successfully applied {version}')
        else:
            error = result.stderr.decode()
            self._publish_status('error', f'Failed: {error}')

    def _publish_status(self, state, message=''):
        """Publish device status"""
        topic = f"devices/{self.device_uuid}/status"
        payload = json.dumps({
            "state": state,
            "message": message,
            "timestamp": time.time()
        })
        self.client.publish(topic, payload)

    def _reboot(self):
        """Reboot system"""
        print("[mqtt] Reboot requested")
        self._publish_status('rebooting', 'System reboot initiated')
        subprocess.run(['systemctl', 'reboot'])

    def _update_collection(self, payload):
        """Update Ansible collection"""
        version = payload['version']
        print(f"[mqtt] Updating collection to {version}")
        # Implementation depends on collection update mechanism

    def run(self):
        """Start MQTT client"""
        # Configure mTLS
        self.client.tls_set(
            ca_certs=self.ca_cert,
            certfile=self.client_cert,
            keyfile=self.client_key,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLSv1_2
        )

        print(f"[mqtt] Connecting to {self.broker}:{self.port}")
        self.client.connect(self.broker, self.port, 60)
        self.client.loop_forever()

if __name__ == '__main__':
    reconciler = MQTTReconciler()
    reconciler.run()
```

### 6. Bootstrap Playbook Enhancements

**Bootstrap playbook moves provisioning files to persistent storage:**

```yaml
# bootstrap.yml (served by console API)

- name: Bootstrap SBNB Device
  hosts: localhost
  connection: local

  tasks:
    # ... existing bootstrap tasks ...

    - name: Move provisioning files to persistent storage
      when: lookup('file', '/tmp/sbnb-provision/device-uuid', errors='ignore')
      block:
        - name: Ensure state directory exists
          file:
            path: /mnt/sbnb-data/state
            state: directory

        - name: Move device UUID
          copy:
            src: /tmp/sbnb-provision/device-uuid
            dest: /mnt/sbnb-data/state/device-uuid
            remote_src: yes

        - name: Move device certificate
          copy:
            src: /tmp/sbnb-provision/device.crt
            dest: /mnt/sbnb-data/state/device.crt
            remote_src: yes

        - name: Move device private key
          copy:
            src: /tmp/sbnb-provision/device.key
            dest: /mnt/sbnb-data/state/device.key
            remote_src: yes
            mode: '0600'

        - name: Cleanup temp files
          file:
            path: /tmp/sbnb-provision
            state: absent
```

---

## Configuration Hierarchy

**Priority (highest to lowest):**

1. `/mnt/sbnb-data/state/mqtt.conf` - Persistent user override
2. `/etc/sbnb/mqtt.conf` - Injected at download time
3. Environment variables - Systemd service overrides
4. Built-in defaults - `console.sbnb.io:8883`

**Example configuration file:**

```ini
# /etc/sbnb/mqtt.conf (injected by console)
MQTT_BROKER=console.sbnb.io
MQTT_PORT=8883
MQTT_CA_CERT=/etc/sbnb/mqtt/ca.crt
MQTT_CLIENT_CERT=/etc/sbnb/mqtt/client.crt
MQTT_CLIENT_KEY=/etc/sbnb/mqtt/client.key
```

---

## Certificate Management

### Bootstrap Certificate (Shared)

**Generated by console or customer:**

```bash
# Generate CA
openssl req -new -x509 -days 3650 -keyout ca.key -out ca.crt \
  -subj "/CN=SBNB CA/O=sbnb/C=US"

# Generate bootstrap certificate (shared across all customer devices)
openssl req -new -newkey rsa:2048 -nodes \
  -keyout bootstrap.key -out bootstrap.csr \
  -subj "/CN=sbnb-bootstrap/O=sbnb/OU=customer-123"

openssl x509 -req -in bootstrap.csr \
  -CA ca.crt -CAkey ca.key \
  -out bootstrap.crt \
  -days 365 -sha256
```

### Device Certificate (Per-Device, CSR-Based)

**Device generates keypair + CSR on boot. Admin signs CSR with CA:**

```bash
# On device (automatic during bootstrap):
openssl genrsa -out /mnt/sbnb-data/state/device.key 2048
openssl req -new -key device.key -out device.csr \
  -subj "/O=SBNB/OU=Devices/CN=${HOSTNAME}"
# CSR is sent in MQTT registration message

# On admin console (approve-devices.sh or manual):
UUID=$(uuidgen)
openssl req -in device.csr -pubkey -noout > device.pub
openssl x509 -new -force_pubkey device.pub \
  -subj "/CN=${UUID}/O=SBNB/OU=Devices" \
  -CA ca.crt -CAkey ca.key \
  -days 3650 -out device.crt
# Only the signed certificate is published back via MQTT
```

**Private key never leaves the device.**

---

## Deployment Scenarios

### Scenario 1: Managed Service (console.sbnb.io)

**User perspective:**
1. Register at console.sbnb.io
2. Download sbnb.raw (credentials pre-injected)
3. Flash USB and boot
4. Device appears in dashboard
5. Configure via web UI
6. Device provisions automatically

**What's injected:**
- Bootstrap certificate (customer-specific or shared)
- CA certificate
- Broker URL: `console.sbnb.io:8883`

### Scenario 2: Self-Hosted (Open Source)

**User perspective:**
1. Build sbnb.raw from source
2. Set up own MQTT broker (Mosquitto, HiveMQ, etc.)
3. Generate own CA + bootstrap certificates
4. Manually inject credentials into image:
   ```bash
   mount -o loop,rw sbnb.raw /mnt/work

   cp my-ca.crt /mnt/work/etc/sbnb/mqtt/ca.crt
   cp my-bootstrap.crt /mnt/work/etc/sbnb/mqtt/bootstrap.crt
   cp my-bootstrap.key /mnt/work/etc/sbnb/mqtt/bootstrap.key

   cat > /mnt/work/etc/sbnb/mqtt.conf <<EOF
   MQTT_BROKER=mqtt.mycompany.com
   MQTT_PORT=8883
   EOF

   umount /mnt/work
   ```
5. Flash and boot
6. Build own console/dashboard
7. Implement own provisioning logic

### Scenario 3: Hybrid

**Use console.sbnb.io services but point to own MQTT broker:**

```bash
# Download image from console, then modify broker URL
mount -o loop,rw sbnb.raw /mnt/work

cat > /mnt/work/etc/sbnb/mqtt.conf <<EOF
MQTT_BROKER=mqtt.mycompany.com
MQTT_PORT=8883
EOF

umount /mnt/work
```

Device uses console certificates but connects to customer broker.

---

## Console Dashboard Features

### Device List View

```
┌───────────────────────────────────────────────────────────┐
│ Devices                                    [+ Add Device]  │
├───────────────────────────────────────────────────────────┤
│ Name           Hostname            Status      Apps        │
│ ML Server      sbnb-525400123456   🟢 Online   Docker,     │
│                                                OpenClaw     │
│ NVR Server     sbnb-525400789abc   🟢 Online   Frigate     │
│ New Device     sbnb-525400def123   ⚠️  Config   -          │
└───────────────────────────────────────────────────────────┘
```

### Device Configuration UI

```
┌─────────────────────────────────────────┐
│ Configure Device                        │
├─────────────────────────────────────────┤
│ Device Name: [My ML Server     ]        │
│ Location:    [Home Lab         ]        │
│                                         │
│ Select Applications:                    │
│ ☑ Docker                                │
│ ☑ NVIDIA GPU Support                    │
│ ☑ OpenClaw (AI Gateway)                 │
│ ☑ Ollama (LLM Runtime)                  │
│ ☐ vLLM (LLM Inference)                  │
│ ☐ Frigate (NVR)                         │
│                                         │
│ Virtual Machines:                       │
│ [+ Add VM]                              │
│                                         │
│ [Cancel] [Save & Provision]             │
└─────────────────────────────────────────┘
```

### Device Detail View

```
┌─────────────────────────────────────────┐
│ ML Server (sbnb-525400123456)           │
├─────────────────────────────────────────┤
│ Status: 🟢 Online                       │
│ Last Seen: 2 minutes ago                │
│ Version: 2025.11.1                      │
│                                         │
│ Applications:                           │
│ • Docker (running)                      │
│ • OpenClaw (running) - Port 18789      │
│ • Ollama (running) - Port 11434        │
│                                         │
│ Virtual Machines:                       │
│ • ubuntu-dev (running)                  │
│   - 4 vCPUs, 8GB RAM                    │
│   - Tailscale: ubuntu-dev.tail1234.ts   │
│                                         │
│ [Edit Configuration] [Remove Device]    │
└─────────────────────────────────────────┘
```

---

## Customer Bundle Structure

**Generated by console based on user selections:**

```yaml
# vars.yml
device_type: bare_metal

apps:
  - docker
  - nvidia_gpu
  - openclaw
  - ollama

openclaw_config:
  port: 18789
  admin_password: "generated-password"

ollama_config:
  port: 11434
  models:
    - llama3.2
    - mistral

vms: []
```

**provision.yml:**

```yaml
- name: Provision SBNB Device
  hosts: localhost
  connection: local

  roles:
    - role: sbnb.compute.storage
    - role: sbnb.compute.networking
    - role: sbnb.compute.docker
      when: "'docker' in apps"

    - role: sbnb.compute.openclaw
      when: "'openclaw' in apps"

    - role: sbnb.compute.ollama
      when: "'ollama' in apps"
```

---

## Implementation Checklist

### Phase 1: Foundation (Non-Breaking)

- [ ] Add `BR2_PACKAGE_PYTHON_PAHO_MQTT=y` to `configs/sbnb_defconfig`
- [ ] Create `/etc/sbnb/mqtt.conf.example` template
- [ ] Create empty `/etc/sbnb/mqtt/` directory in rootfs-overlay
- [ ] Implement `start_mqtt()` function in `boot-sbnb.sh`
- [ ] Create `sbnb-mqtt.service` systemd unit
- [ ] Implement `sbnb-mqtt-reconciler` Python script
- [ ] Test that existing Tailscale deployments are unaffected

### Phase 2: Console Integration

- [ ] Implement image injection endpoint on console.sbnb.io
- [ ] Create dashboard device registration view
- [ ] Implement MQTT broker with mTLS
- [ ] Create device provisioning UI
- [ ] Implement customer bundle generation
- [ ] Add MQTT status monitoring to dashboard

### Phase 3: Testing & Documentation

- [ ] Test bootstrap flow with QEMU
- [ ] Test hybrid mode (Tailscale + MQTT)
- [ ] Document self-hosted deployment
- [ ] Create migration guide for existing users
- [ ] Performance testing (1000+ devices)

### Phase 4: Production Rollout

- [ ] Gradual rollout to new devices
- [ ] Monitor metrics and error rates
- [ ] Collect user feedback
- [ ] Iterate on UX improvements

---

## Open Source Documentation

**README-MQTT.md (to be created):**

```markdown
# MQTT Configuration

SBNB supports MQTT+mTLS for efficient pull-based configuration management.

## Quick Start (console.sbnb.io)

1. Register at console.sbnb.io
2. Download pre-configured image
3. Flash and boot - device appears in dashboard automatically

## Self-Hosted Setup

### 1. Build base image

```bash
cd buildroot
make BR2_EXTERNAL=.. sbnb_defconfig
make -j $(nproc)
```

### 2. Set up MQTT broker

See [docs/MQTT-BROKER-SETUP.md](MQTT-BROKER-SETUP.md) for detailed instructions.

### 3. Inject credentials

```bash
mount -o loop,rw output/images/sbnb.raw /mnt/work

mkdir -p /mnt/work/etc/sbnb/mqtt/
cp ca.crt /mnt/work/etc/sbnb/mqtt/
cp bootstrap.crt /mnt/work/etc/sbnb/mqtt/
cp bootstrap.key /mnt/work/etc/sbnb/mqtt/

cat > /mnt/work/etc/sbnb/mqtt.conf <<EOF
MQTT_BROKER=mqtt.example.com
MQTT_PORT=8883
EOF

umount /mnt/work
```

### 4. Flash and boot

Your devices will connect to your MQTT broker.
```

---

## Benefits Summary

### For Users

- ✅ **Simple onboarding:** Download → Flash → Boot → Configure via web UI
- ✅ **Real-time updates:** Configuration changes applied in <1s (vs 60s polling)
- ✅ **Visibility:** See all devices and their status in real-time
- ✅ **Bi-directional:** Devices can report errors/status back to console
- ✅ **Efficient:** No polling overhead when configuration unchanged

### For Developers

- ✅ **Open source:** Base image contains no proprietary credentials
- ✅ **Self-hostable:** Can run own MQTT broker and console
- ✅ **Non-breaking:** Existing Tailscale deployments continue to work
- ✅ **Flexible:** Support multiple deployment models
- ✅ **Scalable:** MQTT broker can handle thousands of devices efficiently

### For Operations

- ✅ **Better observability:** Real-time device status and metrics
- ✅ **Faster updates:** Push configuration changes instantly
- ✅ **Error reporting:** Devices report provisioning failures immediately
- ✅ **Hybrid mode:** Keep Tailscale for SSH while using MQTT for config

---

## Migration Path

### From REST Polling to MQTT

**Existing devices (no changes):**
- Continue using Tailscale + REST polling
- No forced migration required

**New devices:**
- Automatically use MQTT if credentials injected
- Can still add Tailscale for SSH access

**Gradual migration:**
- Phase 1: Deploy MQTT alongside existing REST reconciler
- Phase 2: Monitor both systems in parallel
- Phase 3: Gradually migrate devices to MQTT
- Phase 4: Deprecate REST polling (keep API for compatibility)

---

## Security Considerations

1. **Certificate Management:**
   - Bootstrap certificate is shared but customer-specific
   - Device certificates are unique per device
   - Short-lived certificates can be implemented (90-day validity)
   - Certificate rotation via MQTT commands

2. **Network Security:**
   - mTLS ensures mutual authentication
   - All communication encrypted (TLS 1.2+)
   - Certificate-based auth prevents key sharing

3. **Credential Storage:**
   - Bootstrap cert embedded in image (read-only filesystem)
   - Device cert stored in /mnt/sbnb-data (persistent, writable, LUKS-encrypted)
   - Private keys generated on device, never transmitted over network (CSR-based provisioning)

4. **CSR-Based Provisioning:**
   - Device generates its own RSA-2048 keypair locally
   - Device sends only CSR (public key) in registration — private key never leaves device
   - Admin/Console signs CSR with CA, sends back only the signed certificate
   - Even if another bootstrap device sniffs the provisioning topic, it gets only a certificate (public info)

5. **Access Control:**
   - MQTT ACL based on certificate CN
   - Devices can only access their own topics
   - Console has admin access to all topics

---

## Future Enhancements

1. **Certificate Auto-Renewal:**
   - Device requests new cert before expiration
   - Console publishes renewed cert via MQTT
   - Zero-downtime certificate rotation

2. **Metrics Collection:**
   - Devices publish metrics to `devices/{uuid}/metrics`
   - Console aggregates and displays in dashboard
   - Integration with Prometheus/Grafana

3. **Remote Shell:**
   - Console can request terminal access via MQTT
   - Device spawns shell and pipes stdin/stdout over MQTT
   - Alternative to Tailscale SSH

4. **Firmware Updates:**
   - Console pushes firmware update notifications
   - Device downloads and applies updates
   - Status reported via MQTT

5. **Multi-Region:**
   - Deploy MQTT brokers in multiple regions
   - Devices connect to nearest broker
   - Console synchronizes state across regions

---

## Conclusion

This architecture provides a path to efficient, real-time device management while maintaining backward compatibility and open source principles. The phased implementation approach allows for gradual rollout and validation at each stage.

**Key Takeaways:**
- MQTT+mTLS provides efficient pull-based configuration management
- Bootstrap pattern (shared cert → device cert) mirrors Tailscale workflow
- Open source friendly: base image contains no credentials
- Self-hostable: users can run their own infrastructure
- Non-breaking: existing deployments continue to work

---

## References

- [Paho MQTT Python Client](https://github.com/eclipse/paho.mqtt.python)
- [MQTT v3.1.1 Specification](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/mqtt-v3.1.1.html)
- [TLS/SSL Configuration Best Practices](https://wiki.mozilla.org/Security/Server_Side_TLS)
- [Certificate Management Best Practices](https://smallstep.com/docs/step-ca/certificate-authority-core-concepts/)
