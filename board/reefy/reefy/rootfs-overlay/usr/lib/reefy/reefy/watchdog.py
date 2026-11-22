"""Liveness checks and bounded recovery for Reefy infrastructure containers.

The systemd timer runs this module once per minute. A single failed probe is
not actionable because devices can briefly lose network or CPU time. Recovery
starts only after consecutive failures and is rate-limited per service.
"""

from datetime import datetime
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request


FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 300
PROBE_TIMEOUT_SECONDS = 3
# The Docker stop grace excludes daemon cleanup and container startup.
RESTART_TIMEOUT_SECONDS = 30
STATE_INSPECT_TIMEOUT_SECONDS = 3
RESTART_OBSERVATION_SECONDS = 10
STATE_DIR = '/run/reefy-watchdog'
COMPOSE_PROJECTS = ('reefy-system', 'state')

CHECKS = (
    {
        'service': 'cloudflared',
        'url': 'http://127.0.0.1:20241/ready',
        'kind': 'cloudflared',
    },
    {
        'service': 'reefy-proxy',
        'url': 'http://127.0.0.1:8080/',
        'kind': 'http-response',
    },
)


def log(message):
    print(f'[reefy-watchdog] {message}', flush=True)


def probe(url, kind, timeout=PROBE_TIMEOUT_SECONDS):
    """Return True when the endpoint proves its event loop is responsive."""
    try:
        request = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if kind != 'cloudflared':
                return True
            payload = json.loads(response.read(4096))
            return (payload.get('status') == 200
                    and payload.get('readyConnections', 0) > 0)
    except urllib.error.HTTPError:
        # reefy-proxy normally rejects an unauthenticated root request with
        # 403. Any complete HTTP response proves its event loop is alive.
        return kind == 'http-response'
    except (json.JSONDecodeError, urllib.error.URLError, OSError, TimeoutError):
        return False


def find_container(service):
    """Find a system Compose service, preferring the Apps v2 project."""
    for project in COMPOSE_PROJECTS:
        try:
            result = subprocess.run(
                [
                    'docker', 'ps', '-aq',
                    '--filter',
                    f'label=com.docker.compose.project={project}',
                    '--filter', f'label=com.docker.compose.service={service}',
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        container = next(
            (line for line in result.stdout.splitlines() if line), None)
        if container:
            return container
    return None


def _read_int(path, default=0):
    try:
        with open(path) as stream:
            return int(stream.read().strip())
    except (OSError, ValueError):
        return default


def _write_int(path, value):
    with open(path, 'w') as stream:
        stream.write(str(value))


def _clear(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _container_state(container, timeout=STATE_INSPECT_TIMEOUT_SECONDS):
    """Read only the exact target's identity, start time and running state."""
    try:
        result = subprocess.run([
            'docker', 'inspect', '--format',
            '{"id":"{{.Id}}","started":"{{.State.StartedAt}}",'
            '"running":{{.State.Running}},"restarting":{{.State.Restarting}}}',
            container], capture_output=True, text=True,
            timeout=timeout)
        if result.returncode:
            return None
        state = json.loads(result.stdout)
        if (not isinstance(state, dict)
                or not re.fullmatch(r'[0-9a-f]{64}', state.get('id', ''))
                or not state['id'].startswith(container)
                or type(state.get('running')) is not bool
                or type(state.get('restarting')) is not bool):
            return None
        started = datetime.fromisoformat(state['started'].replace('Z', '+00:00'))
        if started.tzinfo is None or started.year <= 1:
            return None
        return dict(state, started=started)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError,
            KeyError, AttributeError):
        return None


def _completed_restart(check, container, before, observation_seconds=RESTART_OBSERVATION_SECONDS):
    if before is None:
        return False
    # Docker inspection can briefly block behind the restart's daemon lock.
    # Observe the one submitted restart, without replaying it or extending the
    # watchdog service or end-to-end recovery deadlines. Unknown stays failure.
    deadline = time.monotonic() + observation_seconds
    while (remaining := deadline - time.monotonic()) > 0:
        after = _container_state(container, min(STATE_INSPECT_TIMEOUT_SECONDS, remaining))
        if after and after['id'] != before['id']:
            return False
        if (after and after['started'] > before['started']
                and after['running'] and not after['restarting']):
            remaining = deadline - time.monotonic()
            if remaining > 0 and probe(check['url'], check['kind'],
                                       timeout=min(PROBE_TIMEOUT_SECONDS, remaining)) \
                    and time.monotonic() < deadline:
                return True
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.5, remaining))
    return False


def _write_restart_intent(path, before, expires):
    receipt = {'id': before['id'], 'started': before['started'].isoformat(),
               'expires': expires}
    temporary = path + '.tmp'
    with open(temporary, 'w') as stream:
        json.dump(receipt, stream)
    os.replace(temporary, path)


def _observe_pending(check, path, failure_file, now):
    """Resolve one submitted restart across ticks, never submit it again."""
    try:
        with open(path) as stream:
            receipt = json.load(stream)
        if (not isinstance(receipt, dict) or set(receipt) != {'id', 'started', 'expires'}
                or not isinstance(receipt['id'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', receipt['id'])
                or type(receipt['expires']) is not int or receipt['expires'] <= 0):
            raise ValueError('invalid restart intent')
        started = datetime.fromisoformat(receipt['started'].replace('Z', '+00:00'))
        if started.tzinfo is None or started.year <= 1:
            raise ValueError('invalid restart baseline')
    except (OSError, ValueError, TypeError, AttributeError):
        log(f'{check["service"]}: pending restart receipt unavailable; no restart submitted')
        return
    if now >= receipt['expires']:
        _clear(path)
        log(f'{check["service"]}: restart failed or timed out (pending completion expired)')
        return
    before = {'id': receipt['id'], 'started': started}
    if _completed_restart(check, receipt['id'], before,
                          min(RESTART_OBSERVATION_SECONDS, receipt['expires'] - now)):
        _clear(failure_file)
        _clear(path)
        log(f'{check["service"]}: restart completed')


def check_service(check, state_dir=STATE_DIR, now=None):
    """Probe one service and perform bounded recovery when it stays wedged."""
    service = check['service']
    failure_file = os.path.join(state_dir, f'{service}-failures')
    cooldown_file = os.path.join(state_dir, f'{service}-cooldown')
    pending_file = os.path.join(state_dir, f'{service}-pending.json')
    # A healthy endpoint alone cannot acknowledge an ambiguous mutation.
    # Require the same exact container, a newer start and real liveness.
    # Leave the existing 300s cooldown and 120s oneshot budget unchanged.
    if os.path.lexists(pending_file):
        current = int(time.time()) if now is None else now
        _observe_pending(check, pending_file, failure_file, current)
        return False
    container = find_container(service)

    if not container:
        # The service is optional and may not be present in desired state.
        _clear(failure_file)
        return True

    if probe(check['url'], check['kind']):
        _clear(failure_file)
        return True

    failures = _read_int(failure_file) + 1
    _write_int(failure_file, failures)
    log(f'{service}: liveness probe failed ({failures}/{FAILURE_THRESHOLD})')

    if failures < FAILURE_THRESHOLD:
        return False

    now = int(time.time()) if now is None else int(now)
    cooldown_until = _read_int(cooldown_file)
    if now < cooldown_until:
        log(f'{service}: recovery suppressed by cooldown')
        return False

    log(f'{service}: restarting unresponsive container {container[:12]}')
    before = _container_state(container)
    if before is not None:
        _write_restart_intent(pending_file, before, now + COOLDOWN_SECONDS)
    # Persist the cooldown before submission, including daemon interruption
    # after Docker accepts the command but before its acknowledgment returns.
    _write_int(cooldown_file, now + COOLDOWN_SECONDS)
    pending = False
    try:
        result = subprocess.run(
            ['docker', 'restart', '--time', '5', container],
            capture_output=True,
            text=True,
            timeout=RESTART_TIMEOUT_SECONDS,
        )
        restarted = result.returncode == 0
    except subprocess.TimeoutExpired:
        # Docker may finish the restart before its CLI acknowledgment. Never
        # replay it: require the same container to have a newer start, be
        # running and answer its real liveness probe. Unknown state fails closed.
        restarted = _completed_restart(check, container, before)
        pending = not restarted and before is not None
    except OSError:
        restarted = False

    if restarted:
        _clear(pending_file)
        _clear(failure_file)
        log(f'{service}: restart completed')
    elif pending:
        log(f'{service}: restart completion pending; observing without replay')
    else:
        _clear(pending_file)
        log(f'{service}: restart failed or timed out')
    return False


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    for check in CHECKS:
        check_service(check)


if __name__ == '__main__':
    main()
