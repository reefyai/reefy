"""Persistent Tailscale enrollment independent of app reconciliation."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid

STATE_DIR = Path('/mnt/reefy-data/state/tailscale')
BINDING = STATE_DIR / 'reefy-connection.json'


def durable_storage(path=STATE_DIR):
    try:
        target = path if path.exists() else path.parent
        result = subprocess.run(['findmnt', '-n', '-T', str(target), '-o', 'FSTYPE'],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() in ('ext4', 'xfs', 'btrfs')


def prepare_state():
    if not durable_storage():
        raise RuntimeError('storage_unavailable')
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    # Upgrade an existing daemon's identity only into an empty destination.
    old = Path('/var/lib/tailscale')
    if old.is_dir() and not any(STATE_DIR.iterdir()):
        import shutil
        for source in old.iterdir():
            destination = STATE_DIR / source.name
            if source.is_dir():
                shutil.copytree(source, destination)
            elif source.is_file():
                shutil.copy2(source, destination)
        for item in STATE_DIR.rglob('*'):
            item.chmod(0o700 if item.is_dir() else 0o600)


class Tailscale:
    def __init__(self, publish):
        self.publish = publish
        self.lock = threading.Lock()
        self.last_request = 0
        self.request_id = str(uuid.uuid4())
        self.error = None

    def _binding(self):
        try:
            return json.loads(BINDING.read_text())
        except (OSError, ValueError):
            return {}

    def _raw_status(self):
        try:
            result = subprocess.run(['tailscale', 'status', '--json'],
                                    capture_output=True, text=True, timeout=5)
            return json.loads(result.stdout) if result.returncode == 0 else {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {}

    def status(self):
        result = {'supported': True, 'state': 'needs_login'}
        if not durable_storage():
            return dict(result, state='storage_unavailable', error='storage_unavailable')
        raw = self._raw_status()
        binding = self._binding()
        state = raw.get('BackendState')
        own = raw.get('Self') or {}
        if state == 'Running':
            result['state'] = 'connected' if binding else 'unmanaged'
        elif state == 'NeedsMachineAuth':
            result['state'] = 'needs_approval'
        elif state == 'Stopped':
            result['state'] = 'stopped'
        elif not raw:
            result.update(state='error', error='daemon_unavailable')
        if own.get('ID') and not binding:
            result['state'] = 'unmanaged'
        if binding.get('connection_id'):
            result['connection_id'] = binding['connection_id']
        result['ips'] = own.get('TailscaleIPs') or []
        result['dns_name'] = own.get('DNSName', '').rstrip('.')
        if self.error and result['state'] not in ('connected', 'needs_approval', 'unmanaged'):
            result.update(state='error', error=self.error)
        return result

    def tick(self, config):
        if not config or not config.get('enabled') or not config.get('connection_id'):
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            if not durable_storage():
                return
            state = self.status()
            if state['state'] in ('connected', 'needs_approval', 'unmanaged', 'stopped'):
                return
            subprocess.run(['systemctl', 'start', 'tailscaled.service'],
                           capture_output=True, timeout=10)
            if time.monotonic() - self.last_request < 60:
                return
            self.last_request = time.monotonic()
            self.publish('tailscale/enroll', {
                'connection_id': config['connection_id'], 'request_id': self.request_id})
        finally:
            self.lock.release()

    def enroll(self, payload, config):
        with self.lock:
            if (not config or not config.get('enabled')
                    or payload.get('connection_id') != config.get('connection_id')
                    or payload.get('request_id') != self.request_id):
                return
            if payload.get('error'):
                self.error = 'credential_unavailable'
                return
            key = payload.get('auth_key')
            if not isinstance(key, str) or not key.startswith('tskey-auth-') or len(key) > 512:
                return
            prepare_state()
            state = self.status()
            if state['state'] in ('connected', 'needs_approval', 'unmanaged', 'stopped'):
                self.publish('tailscale/result', state)
                return
            binding = self._binding()
            if binding and binding.get('connection_id') != config['connection_id']:
                self.error = 'connection_changed'
                return
            # Save intent before login, so a crash between login and acknowledgment
            # resumes the same identity rather than enrolling another node.
            temporary = BINDING.with_suffix('.tmp')
            with open(temporary, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({'connection_id': config['connection_id']}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, BINDING)
            directory = os.open(STATE_DIR, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            try:
                # The secret never appears in argv, logs, or persistent state.
                with tempfile.NamedTemporaryFile(mode='w', dir='/run', prefix='reefy-ts-') as secret:
                    secret.write(key)
                    secret.flush()
                    subprocess.run(['tailscale', 'up', '--auth-key=file:' + secret.name,
                                    '--accept-dns=false', '--accept-routes=false',
                                    '--ssh=false', '--timeout=30s'],
                                   capture_output=True, timeout=35)
                state = self.status()
                self.error = None if state['state'] in ('connected', 'needs_approval') else 'enrollment_failed'
            except (OSError, subprocess.TimeoutExpired):
                self.error = 'enrollment_failed'
            self.publish('tailscale/result', self.status())
