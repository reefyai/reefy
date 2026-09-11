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

from reefy.storage_pressure import PressureError, QUANTUM
from reefy.storage_quota import Registry, RUN_DIR, command, state_lock


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
