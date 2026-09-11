"""Short, durable reservations for managed operations sharing one thin pool.

A lease is not a free-space observation. Its owner waits for a guard pass to
revoke competing allowances and acknowledge it before starting work. Crashed
owners leave a conservative reservation; boot recovery may release it only
once the operation and its writers have been reconciled.
"""
from contextlib import contextmanager
import json
import os
import signal
import time
import uuid
from pathlib import Path

from reefy.storage_pressure import PressureError, QUANTUM
from reefy.storage_quota import Registry, RUN_DIR, command, state_lock


def boot_identity():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() or None
    except FileNotFoundError:
        return None  # Unknown identity never authorizes lease reclamation.


def release_previous_boot_leases(registry):
    """Old-boot processes cannot still write; PIDs alone are not sufficient.

    Call during boot activation before writers start. Same-boot and unknown
    leases remain reserved, including a dead parent with a surviving child.
    """
    current = boot_identity()
    if not current:
        raise PressureError('cannot establish storage lease boot identity')
    leases = registry.data.get('leases', {})
    stale = [identity for identity, lease in leases.items()
             if lease.get('boot_id') and lease['boot_id'] != current]
    for identity in stale:
        del leases[identity]
    if stale:
        registry.data['generation'] = registry.data.get('generation', 0) + 1
        registry.save()
    return len(stale)


def wait_generation(generation, *, lease=None, volume=None, timeout=20):
    pid = int(command(['systemctl', 'show', '--property=MainPID', '--value',
                       'reefy-storage-guard.service']).strip())
    if pid <= 1:
        raise PressureError('storage guard is unavailable')
    os.kill(pid, signal.SIGUSR1)
    deadline = time.monotonic() + timeout
    while True:
        if os.path.exists(RUN_DIR + '/hold.json'):
            raise PressureError('storage writers are held pending recovery')
        try:
            with open(RUN_DIR + '/status.json') as stream:
                status = json.load(stream)
            if status.get('generation', -1) >= generation:
                if status['allocation']['quiesce']:
                    raise PressureError('physical capacity cannot admit this operation')
                if lease and lease not in status.get('admitted_leases', []):
                    raise PressureError('operation exceeds its storage class ceiling')
                if volume and volume not in status['allocation']['limits']:
                    raise PressureError('volume missing from verified allocation')
                return status
        except FileNotFoundError:
            pass
        if time.monotonic() >= deadline:
            raise PressureError('storage allocation timed out')
        time.sleep(0.1)


@contextmanager
def reservation(kind, budget, *, storage_class='runtime', target=None):
    """Reserve a bounded peak, optionally making it available to one project.

    target is a registry identity, not an arbitrary filesystem path. No lock
    survives the yield. Normal quotas continue to bound the operation's writes.
    """
    if type(budget) is not int or budget <= 0 or budget % QUANTUM:
        raise ValueError('reservation requires a positive 4-KiB-aligned budget')
    if storage_class not in ('bulk', 'runtime', 'state'):
        raise ValueError('unknown admission class')
    if not Registry().data.get('active', False):
        yield
        return
    identity = uuid.uuid4().hex
    registered = False
    with state_lock():
        registry = Registry()
        active = registry.data.get('active', False)
        if active:
            if (registry.data.get('activation_pending')
                    or not registry.data.get('inventory_complete')
                    or os.path.exists(RUN_DIR + '/hold.json')):
                raise PressureError('storage policy is not ready for an operation')
            if target and (target not in registry.data['projects']
                           or registry.data['projects'][target].get('retired')):
                raise PressureError('reservation destination is not active')
            registry.data.setdefault('leases', {})[identity] = {
                'kind': kind, 'bytes': budget, 'storage_class': storage_class,
                'target': target, 'pid': os.getpid(),
                'boot_id': boot_identity(),
            }
            generation = registry.data.get('generation', 0) + 1
            registry.data['generation'] = generation
            registry.save()
            registered = True
    try:
        if active:
            wait_generation(generation, lease=identity)
        yield
    finally:
        if registered:
            with state_lock():
                registry = Registry()
                registry.data.get('leases', {}).pop(identity, None)
                registry.data['generation'] = registry.data.get('generation', 0) + 1
                registry.save()


@contextmanager
def quiesced_reservation(kind, budget, *, storage_class='state'):
    """Bound boot initialization before the normal guard can start.

    Docker ExecStartPre has no daemon MainPID yet. The app-volume boot unit
    precedes Docker and the reconciler, so waiting for guard admission there
    would deadlock its own startup dependencies. Only this verified quiescent
    path uses a physical budget directly; live operations use durable leases.
    """
    from reefy.storage_pressure import boundaries
    from reefy.storage_quota import physical_sample
    if type(budget) is not int or budget <= 0 or budget % QUANTUM:
        raise ValueError('invalid quiesced allocation budget')
    if storage_class not in ('bulk', 'runtime', 'state'):
        raise ValueError('unknown admission class')
    for unit in ('docker.service', 'reefy-reconciler.service', 'reefy-backup.service'):
        pid = int(command(['systemctl', 'show', '--property=MainPID', '--value', unit]).strip() or '0')
        if pid:
            raise PressureError('offline storage admission requires stopped app writers')
    sample = physical_sample()
    ceiling = getattr(boundaries(sample.capacity), storage_class)
    if (not sample.healthy or sample.metadata_used * 100 >= sample.metadata_capacity * 80
            or sample.used + budget + 64 * 1024**2 >= ceiling):
        raise PressureError('insufficient physical headroom for boot initialization')
    yield
    after = physical_sample()
    if not after.healthy or after.used >= ceiling:
        raise PressureError('boot initialization exhausted its class headroom')
