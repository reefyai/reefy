# Desired State Architecture

## Overview

Desired state is the mechanism for configuring adopted devices. The server builds a JSON state object containing the device's full configuration (hostname, Docker compose, WiFi, storage, app volumes), pushes it to the device via MQTT, and the device reconciler applies it idempotently.

## Data Flow

```
Server (reefy-service)                          Device (sbnb-mqtt-reconciler)
       |                                                |
       |-- build_desired_state(uuid)                    |
       |   Queries DB: device, instances, apps          |
       |   Generates compose + routes + proxy           |
       |                                                |
       |-- MQTT: {action: apply_state, state: {...}} -->|
       |   Topic: reefy/{pub_id}/devices/{uuid}/commands|
       |                                                |
       |                                  Save to desired-state.json
       |                                  Set stage → 'applying'
       |                                  Apply hostname
       |                                  Apply WiFi (if present)
       |                                  Apply storage (LUKS/LVM)
       |                                  Prepare app dirs (chown)
       |                                  docker compose up -d
       |                                  Publish state_hash
       |                                  Wait for tunnel health
       |                                  Set stage → 'ready'
       |                                                |
       |<-- state_hash (retained) ----------------------|
       |    Topic: reefy/{pub_id}/devices/{uuid}/state_hash
```

## State Object Structure

```json
{
  "hostname": "seahorse",
  "compose": {
    "services": {
      "cloudflared": { "image": "cloudflare/cloudflared:latest", "..." },
      "ttyd": { "image": "tsl0922/ttyd:latest", "..." },
      "reefy-proxy": { "image": "ghcr.io/reefyai/reefy-proxy:latest", "..." },
      "<instance-name>": { "image": "...", "ports": [...], "volumes": [...] },
      "<instance-name>-tty": { "image": "ghcr.io/reefyai/reefy-app-sidecar-ttyd:latest", "..." }
    }
  },
  "app_volumes": [
    { "path": "/mnt/sbnb-data/apps/<instance>/<vol>", "uid": 1000, "seed_files": {"...": "base64..."} }
  ],
  "wifi": { "ssid": "MyNetwork", "password": "..." },
  "storage": {
    "devices": [{ "path": "/dev/nvme0n1", "..." }]
  }
}
```

## Server Side (`app/services/desired_state.py`)

### `build_desired_state(device_uuid)`

Builds the full state by:
1. Querying device info (name, tunnel_id, tunnel_token, auth_secret, storage/wifi config)
2. Querying device_instances (app deployments with ports)
3. Loading app definitions from `apps/*/app.json` catalog
4. Generating compose services:
   - **cloudflared** — Cloudflare tunnel connector (if tunnel_token exists)
   - **ttyd** — Host terminal (always present)
   - **reefy-proxy** — Auth reverse proxy with route table (if auth_secret exists)
   - **User apps** — From device_instances, with port mapping, volumes, GPU, env vars
   - **App tty sidecars** — Per-app terminal containers (if tty_port allocated)
5. Computing routes for both tunnel (Cloudflare) and LAN (HTTPS) access

### Port allocation

- User app ports start at 10001+, allocated sequentially per device
- Internal ports are offset by +10000 (e.g., host_port=10001 → internal=20001)
- LAN HTTPS proxy fronts the original ports with TLS + JWT auth
- Host terminal: LAN port 7682 → ttyd at 127.0.0.1:7681

### `compute_state_hash(state)`

SHA-256 of `json.dumps(state, sort_keys=True)`, truncated to 16 hex chars. Must match device-side implementation.

## When State is Pushed

State is pushed via MQTT `apply_state` command in these scenarios:

1. **Device adoption** — Server calls `build_desired_state()` and publishes immediately
2. **Device comes online** — Server compares device's `state_hash` with server-computed hash; pushes only if they differ (dedup)
3. **Config change** — User modifies services, WiFi, storage, or device name via dashboard → server pushes updated state
4. **Manual sync** — User clicks "Sync State" in dashboard

## Device Side (`sbnb-mqtt-reconciler`)

### File paths

| Path | Purpose |
|------|---------|
| `/mnt/sbnb-data/state/desired-state.json` | Persisted desired state (survives reboots) |
| `/mnt/sbnb-data/state/docker-compose.json` | Generated compose file (written from state) |

### Apply sequence (`_apply_desired_state`)

1. **Hostname** — `hostnamectl set-hostname` (or revert to MAC-based default)
2. **WiFi** — Calls `wifi-setup <ssid> <password>` script (before compose, connectivity may be needed for image pulls)
3. **Storage** — LUKS + LVM setup for encrypted extra data drives
4. **App volumes** — Creates host directories and chowns to correct UID; writes seed files (base64-decoded) on first run
5. **Docker compose** — Writes `docker-compose.json`, runs `docker compose up -d --pull always --remove-orphans`

### Concurrency

`_apply_lock` (threading.Lock) prevents parallel `apply_state` runs. If a second command arrives while one is in progress, it is skipped with a log message.

### Boot-time apply

On device connect (`_handle_device_connect`), if `desired-state.json` exists, the reconciler re-applies the saved state. This ensures the device converges to the desired state after reboots without needing the server to re-push.

### State hash dedup

After applying state, the device publishes its state hash on a retained MQTT topic. The server reads this hash on the next device heartbeat and only pushes state if the hash has changed. This avoids redundant apply cycles.

## Error Handling and Recovery

### Compose failure recovery (`_diagnose_compose_failure`)

When `docker compose up` fails, the reconciler analyzes the output and attempts automated recovery:

| Error pattern | Recovery action |
|---------------|----------------|
| `failed to register layer`, `no such file or directory`, `layer does not exist`, `error creating overlay mount`, `failed to mount overlay` | `docker system prune -a -f --volumes` (clear all images, re-pull) |
| `no space left on device` | `docker system prune -a -f` (free disk space) |

Recovery runs **once** per apply attempt. After recovery, compose is retried immediately (no backoff delay).

### Retry policy

- 5 attempts with exponential backoff (10s, 20s, 40s, 80s, 160s)
- Recovery action (if triggered) replaces the backoff delay for that attempt
- On final failure: stage is set to `error` and reported to server

### Stage reporting

The device reports its stage via MQTT throughout the apply process:

| Stage | Meaning |
|-------|---------|
| `applying` | Desired state apply in progress |
| `ready` | All services running, tunnel healthy |
| `error` | Apply failed (compose failure after retries) |
| `updating` | Firmware update in progress |
