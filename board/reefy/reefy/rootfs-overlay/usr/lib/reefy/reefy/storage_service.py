"""Storage guard process, independent watchdog and activation coordinator.

Invoked by systemd and the reconciler. Public policy arrives only through the
validated desired-state contract; runtime files are internal coordination state.
"""
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time

from reefy import shared
from reefy.storage import Storage
from reefy.storage_admission import wait_generation
from reefy.storage_guard import Guard
from reefy.storage_migration import Migration
from reefy.storage_policy import storage_policy
from reefy.storage_pressure import PressureError, QUANTUM
from reefy.storage_quota import (
    Registry, RUN_DIR, atomic_json, command, physical_sample, state_lock,
    FileAttributes, PROJINHERIT, mount_info, require_enforcement, read_quotas, set_quota,
    verify_tree, assign_tree, flush_filesystem,
)
from reefy.storage_runtime import configure_daemon, register_runtime
from reefy.storage_watchdog import Writers, check


# Initial envelope for qualification. A measured larger rate increases the
# reserve durably; it is never lowered automatically. Physical containment on
# representative hardware remains a release gate, not a guarantee of statfs.
INITIAL_RATE = 128 * 1024**2
RESPONSE_SECONDS = 30
IN_FLIGHT = 64 * 1024**2
STALE_SECONDS = 20


def notify(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.sendto(message.encode(), address)


def read_policy(state=None):
    registry = Registry()
    if state is None:
        try:
            with open(shared.desired_state_path()) as source:
                state = json.load(source)
        except FileNotFoundError:
            state = {}
    previous = registry.data.get('volume_classes', {})
    return storage_policy(state, active=registry.data.get('active', False), previous=previous)


def new_guard():
    return Guard(peak_bytes_per_second=INITIAL_RATE, response_seconds=RESPONSE_SECONDS,
                 in_flight_bytes=IN_FLIGHT)


def validate_activation_layout(policies):
    """Reject unsupported existing filesystems before changing cached policy.

    A class-aware firmware image does not convert legacy ext4/f2fs data. Those
    devices retain their identified legacy configuration until a separate,
    data-preserving filesystem migration is performed.
    """
    mount_info('/mnt/reefy-data')
    storage = Storage()
    metadata = storage._lv_metadata_names()
    if metadata is None or storage.STORAGE_POOL not in metadata:
        raise PressureError('storage quotas require an available thin pool')
    for path in policies:
        lv = storage._volume_lv_name(path)
        if lv in metadata and storage._fs_type(f'/dev/{storage.STORAGE_VG}/{lv}') != 'xfs':
            raise PressureError('existing app filesystem requires a separate XFS migration')
        if os.path.exists(path):
            mount_info(path)


def requires_activation(state):
    policies = read_policy(state)
    if policies is None:
        return False
    registry = Registry()
    return (not registry.data.get('active')
            or registry.data.get('activation_pending', False))


def request_activation():
    # The separate coordinator stops Docker and the data plane. It must not
    # stop the process which is executing the synchronous control RPC itself.
    command(['systemctl', 'start', '--no-block', 'reefy-storage-activate.service'])


def ensure_volume(path, storage_class):
    """Establish an empty new volume before seeds, extraction or container use."""
    if os.path.exists(RUN_DIR + '/hold.json'):
        raise PressureError('storage writers are held pending recovery')
    with state_lock():
        registry = Registry()
        if not registry.data.get('active'):
            raise PressureError('volume policy is not active')
        mount = mount_info(path)
        require_enforcement(mount['target'])
        identity = mount['uuid'] + ':' + path
        record = registry.data['projects'].get(identity)
        attributes = FileAttributes()
        if record is None:
            if os.path.realpath(path) != path:
                raise PressureError('new volume root cannot be a symlink')
            with os.scandir(path) as entries:
                if next(entries, None) is not None:
                    raise PressureError('nonempty unregistered volume requires controlled migration')
            quotas = read_quotas(mount['target'])
            _, record = registry.register(path, mount, storage_class, quotas)
            attributes.assign(path, record['project'], True)
            set_quota(mount['target'], record['project'], QUANTUM)
            if read_quotas(mount['target']).get(record['project'], {}).get('hard') != QUANTUM:
                raise PressureError('new volume quota could not be verified')
            record.update(complete=True, ownership_version=2, root_inode=os.lstat(path).st_ino)
        else:
            flags, _, _, project, _ = attributes.read(path)
            if (not record.get('complete') or project != record['project']
                    or not flags & PROJINHERIT
                    or record.get('root_inode') != os.lstat(path).st_ino):
                raise PressureError('volume ownership requires controlled recovery')
            quota = read_quotas(mount['target']).get(project, {})
            if not quota.get('hard') or quota.get('soft'):
                raise PressureError('volume quota requires recovery')
            if (record['storage_class'] == storage_class and not record.get('retired')
                    and record['device'] == mount['maj:min']):
                return
        record['retired'] = False
        record['storage_class'] = storage_class
        record.update(mount=mount['target'], device=mount['maj:min'])
        registry.data.setdefault('volume_classes', {})[path] = storage_class
        generation = registry.data.get('generation', 0) + 1
        registry.data['generation'] = generation
        registry.save()
    wait_generation(generation, volume=identity)


def retire_missing(paths):
    """Close removed volumes before their filesystems can be unmounted."""
    registry = Registry()
    if not registry.data.get('active'):
        return
    ids = command(['docker', 'container', 'ls', '--all', '--quiet']).split()
    mounts = []
    if ids:
        for offset in range(0, len(ids), 100):
            rows = json.loads(command(['docker', 'inspect', *ids[offset:offset+100]]))
            mounts.extend(mount.get('Source', '') for row in rows for mount in row.get('Mounts', []))
    with state_lock():
        registry = Registry()
        for record in registry.data['projects'].values():
            path = record['path']
            if (not path.startswith('/mnt/reefy-data/apps/')
                    or path in paths or record.get('retired')):
                continue
            if any(source and (source == path or source.startswith(path + '/')
                               or path.startswith(source.rstrip('/') + '/'))
                   for source in mounts):
                raise PressureError('removed volume is still referenced by a container')
            quota = read_quotas(record['mount']).get(record['project'])
            if quota is None:
                raise PressureError('removed volume quota is unavailable')
            hard = max(QUANTUM, quota['used'])
            set_quota(record['mount'], record['project'], hard)
            if read_quotas(record['mount']).get(record['project'], {}).get('hard') != hard:
                raise PressureError('removed volume allowance could not be revoked')
            record['retired'] = True
            registry.data.get('volume_classes', {}).pop(path, None)
        registry.save()


def verify_restore(paths, *, repair=False):
    """Validate destination ownership before startup, including cached restores.

    File-level extraction normally inherits the destination assignment. Repair
    imported metadata only while that destination has no running container.
    Never retag a live source or follow a hardlink out of the destination tree.
    """
    registry = Registry()
    if not registry.data.get('active'):
        return
    for path in paths:
        mount = mount_info(path)
        record = registry.data['projects'].get(mount['uuid'] + ':' + path)
        if not record or not record.get('complete') or record.get('retired'):
            raise PressureError('restore destination is not registered')
        require_enforcement(mount['target'])
        excluded = set(paths) | {row['path'] for row in registry.data['projects'].values()
                                 if not row.get('retired')}
        try:
            verify_tree(path, record['project'], excluded)
        except PressureError:
            if not repair:
                raise
            ids = command(['docker', 'container', 'ls', '--quiet']).split()
            for offset in range(0, len(ids), 100):
                containers = json.loads(command(['docker', 'inspect', *ids[offset:offset + 100]]))
                for container in containers:
                    for bind in container.get('Mounts', []):
                        source = bind.get('Source', '').rstrip('/')
                        if source and (source == path or source.startswith(path + '/')
                                       or path.startswith(source + '/')):
                            raise PressureError('restore destination is still in use')
            # XFS charges transferred blocks to the destination as each inode
            # is assigned. An insufficient destination quota fails the restore;
            # no temporary unlimited quota or source limit change is needed.
            assign_tree(path, record['project'], excluded)
            flush_filesystem(path)
            verify_tree(path, record['project'], excluded)
        quota = read_quotas(mount['target']).get(record['project'], {})
        if not quota.get('hard') or quota.get('soft'):
            raise PressureError('restore destination lacks a verified quota')


def activate(*, boot=False):
    policies = read_policy()
    if policies is None:
        return
    validate_activation_layout(policies)
    registry = Registry()
    registry.data['activation_pending'] = True
    registry.data['inventory_complete'] = False
    registry.save()
    atomic_json(RUN_DIR + '/hold.json', {'reason': 'quota activation in progress'})
    # Boot runs in Docker ExecStartPre, before any Docker writer exists. Live
    # first activation runs outside Docker, after the new policy was persisted.
    if not boot:
        for unit in ('reefy-reconciler.service', 'reefy-backup.service',
                     'docker.socket', 'docker.service'):
            loaded = command(['systemctl', 'show', '--property=LoadState', '--value', unit]).strip()
            if loaded != 'not-found':
                command(['systemctl', 'stop', unit], timeout=120)
    storage = Storage()
    storage.boot_mount(quiesced_admission=True)
    metadata = storage._lv_metadata_names()
    if metadata is None:
        raise PressureError('cannot verify owned app volume mounts')
    for path in policies:
        if (storage._volume_lv_name(path) in metadata
                and storage._dedicated_volume_mount_status(path) is not True):
            raise PressureError('owned app LV is unavailable; refusing directory fallback')
        os.makedirs(path, mode=0o755, exist_ok=True)
    all_policies = {'/mnt/reefy-data': 'runtime', **policies}
    all_policies.update(register_runtime(registry))
    migration = Migration(registry=registry)
    migration.prepare(all_policies)
    for record in registry.data['projects'].values():
        record['retired'] = record['path'] not in all_policies
    registry.data.update(active=True, inventory_complete=True,
                         volume_classes=policies)
    registry.save()
    result = new_guard().pass_once()
    if result['allocation']['quiesce']:
        raise PressureError('activation completed but physical capacity is unsafe')
    command(['systemctl', 'is-active', '--quiet', 'reefy-storage-watchdog.service'])
    Writers().thaw()
    Path(RUN_DIR + '/hold.json').unlink(missing_ok=True)
    atomic_json(RUN_DIR + '/session.json', {'ready': True})
    registry.data['activation_pending'] = False
    registry.save()
    if not boot:
        command(['systemctl', 'start', '--no-block', 'docker.service', 'reefy-reconciler.service'])


def boot_gate():
    policy = read_policy()
    configure_daemon(policy is not None)
    if policy is None:
        return
    # Verification is mandatory on every boot. Completed records skip recursive
    # walks; a prior interrupted activation resumes with the persisted IDs.
    activate(boot=True)


def run_guard():
    wake = threading.Event()
    signal.signal(signal.SIGUSR1, lambda *_: wake.set())
    guard = new_guard()
    while True:
        started = time.monotonic()
        try:
            registry = Registry()
            if (registry.data.get('active') and not registry.data.get('activation_pending')
                    and os.path.exists(RUN_DIR + '/session.json')):
                result = guard.pass_once()
                if result['allocation']['quiesce']:
                    check(active=True, stale_seconds=STALE_SECONDS)
        except Exception as error:
            print(f'[storage-guard] {type(error).__name__}: {error}', flush=True)
            check(active=True, stale_seconds=0)
        wake.wait(max(0.1, 10 - (time.monotonic() - started)))
        wake.clear()


def run_watchdog():
    notify('READY=1')
    while True:
        started = time.monotonic()
        try:
            registry = Registry()
            check(active=registry.data.get('active', False)
                  and not registry.data.get('activation_pending', False)
                  and os.path.exists(RUN_DIR + '/session.json'),
                  stale_seconds=STALE_SECONDS)
        except Exception as error:
            print(f'[storage-watchdog] {type(error).__name__}: {error}', flush=True)
            check(active=True, stale_seconds=0)
        notify('WATCHDOG=1')
        time.sleep(max(0.05, 1 - (time.monotonic() - started)))


def recover():
    """Resume held writers only after a fresh guard and verified quota pass."""
    registry = Registry()
    if not registry.data.get('active') or registry.data.get('activation_pending'):
        raise PressureError('incomplete activation requires migration recovery')
    if not os.path.exists(RUN_DIR + '/hold.json'):
        return
    # A stopped or wedged guard cannot prove its own recovery. systemd tears
    # down the old process (and releases its flock) before starting a fresh one.
    command(['systemctl', 'restart', 'reefy-storage-guard.service'], timeout=20)
    command(['systemctl', 'is-active', '--quiet', 'reefy-storage-watchdog.service'])
    result = new_guard().pass_once()
    if result['allocation']['quiesce']:
        raise PressureError('physical pressure still prevents writer recovery')
    Writers().thaw()
    Path(RUN_DIR + '/hold.json').unlink(missing_ok=True)


def force_hold():
    if os.path.exists(RUN_DIR + '/session.json'):
        # OnFailure must not retry the external sampler that may have wedged.
        atomic_json(RUN_DIR + '/hold.json', {'reason': 'storage watchdog failed'})
        Writers().freeze()


def main():
    actions = {'guard': run_guard, 'watchdog': run_watchdog,
               'activate': activate, 'boot': boot_gate,
               'hold': force_hold, 'recover': recover}
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        raise SystemExit('expected internal storage role')
    actions[sys.argv[1]]()


if __name__ == '__main__':
    main()
