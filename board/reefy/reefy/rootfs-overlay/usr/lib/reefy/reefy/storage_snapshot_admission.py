"""Reserve complete snapshot COW before a short, coordinated creation barrier.

The worker runs outside app/backup cgroups. The existing hold/recovery protocol
keeps writers stopped until source allocation and all quotas are revalidated.
Borg transfers happen after writers resume. Runtime requests are private state.
"""
import hashlib
import json
import os
from pathlib import Path
import runpy

from reefy.shared import STORAGE_VG, STORAGE_POOL
from reefy.storage_pressure import PressureError, QUANTUM
from reefy.storage_quota import Registry, RUN_DIR, atomic_json, command, mount_info

REQUEST = RUN_DIR + '/snapshot-request.json'
UNIT = 'reefy-storage-snapshot.service'
SETUP_BYTES = 64 * 1024**2


def mapped_bytes(device):
    """Read actual allocated thin sectors, never rounded LVM percentages."""
    table = command(['dmsetup', 'table', device], timeout=2).split()
    status = command(['dmsetup', 'status', device], timeout=2).split()
    try:
        pool = os.stat(f'/dev/mapper/{STORAGE_VG}-{STORAGE_POOL}-tpool').st_rdev
        expected = f'{os.major(pool)}:{os.minor(pool)}'
        if (len(table) != 5 or table[0] != '0' or table[2] != 'thin'
                or table[3] != expected or len(status) != 5
                or status[:3] != table[:3]):
            raise ValueError('unexpected thin target')
        mapped, length = int(status[3]), int(table[1])
        if not 0 <= mapped <= length:
            raise ValueError('invalid mapped sector count')
        return mapped * 512
    except (ValueError, IndexError) as error:
        raise PressureError('cannot establish snapshot allocation') from error


def source_inventory(paths):
    registry = Registry()
    if not paths or len(paths) != len(set(paths)):
        raise PressureError('snapshot sources must be nonempty and unique')
    sources = []
    for path in paths:
        records = [r for r in registry.data['projects'].values()
                   if r['path'] == path and not r.get('retired')]
        if len(records) != 1 or not records[0].get('complete'):
            raise PressureError('snapshot source ownership is incomplete')
        record = records[0]
        device = f'/dev/{STORAGE_VG}/reefy_backup_' + hashlib.sha1(path.encode()).hexdigest()[:12]
        mount = mount_info(path)
        if (mount['target'] != path or record['mount'] != path
                or mount['uuid'] != record['filesystem']
                or mount['maj:min'] != record['device']
                or os.path.realpath(mount['source']) != os.path.realpath(device)):
            raise PressureError('snapshot source is not its verified dedicated LV')
        sources.append({'path': path, 'device': device,
                        'bytes': mapped_bytes(device),
                        'storage_class': record['storage_class']})
    return sources


def reservation_size(paths):
    sources = source_inventory(paths)
    # Growth before the barrier may exceed this small slack. In that case the
    # worker rejects the backup before creating any snapshot and resumes apps.
    budget = SETUP_BYTES + sum(s['bytes'] + SETUP_BYTES for s in sources)
    budget = ((budget + QUANTUM - 1) // QUANTUM) * QUANTUM
    priority = {'bulk': 0, 'runtime': 1, 'state': 2}
    storage_class = min((s['storage_class'] for s in sources), key=priority.__getitem__)
    return budget, storage_class


def pending_preparation():
    try:
        value = json.loads(Path(REQUEST).read_text())
    except FileNotFoundError:
        return False
    if not isinstance(value, dict) or type(value.get('finished')) is not bool:
        raise PressureError('snapshot request evidence is unreadable')
    return not value['finished']


def create_reserved(paths, instance_uuid, timestamp, lease):
    if pending_preparation():
        raise PressureError('previous snapshot preparation needs recovery')
    atomic_json(REQUEST, {'paths': paths, 'instance_uuid': instance_uuid,
                         'timestamp': timestamp, 'lease': lease, 'finished': False})
    # The caller may itself be frozen by this worker. Its wait is longer than
    # the worker's systemd deadline; no preparation subprocess runs in the
    # caller's cgroup after its resource lease can be released.
    command(['systemctl', 'start', UNIT], timeout=150)
    result = json.loads(Path(REQUEST).read_text())
    if result.get('lease') != lease or not result.get('finished'):
        raise PressureError('snapshot preparation did not complete')
    if result.get('error'):
        raise PressureError(result['error'])
    return [tuple(row) for row in result['snapshots']]


def abort_preparation():
    """Recovery stops the producer before treating absence as reclamation."""
    if not Path(REQUEST).exists():
        return False
    request = json.loads(Path(REQUEST).read_text())
    active = command(['systemctl', 'show', '--property=ActiveState', '--value', UNIT]).strip()
    interrupted = not request.get('finished') or active not in ('inactive', 'failed')
    command(['systemctl', 'stop', UNIT], timeout=15)
    events = Path('/sys/fs/cgroup/system.slice', UNIT, 'cgroup.events')
    if events.exists() and 'populated 1' in events.read_text():
        raise PressureError('snapshot worker still has live processes')
    if interrupted:
        request.update(finished=True, error='snapshot preparation interrupted')
        atomic_json(REQUEST, request)
    return interrupted


def coordinate():
    from reefy.storage_watchdog import hold_writers, hold_lock, finish_hold, Writers
    from reefy.storage_service import recover
    from reefy.storage_snapshots import cleanup_orphans
    request = json.loads(Path(REQUEST).read_text())
    if request.get('finished'):
        return
    lease = Registry().data.get('leases', {}).get(request['lease'])
    if (not lease or not lease.get('admitted') or lease['kind'] != 'backup-snapshots'
            or lease['bytes'] <= SETUP_BYTES):
        raise PressureError('snapshot worker lacks an admitted reservation')
    snapshots = []
    try:
        hold_writers('preparing reserved backup snapshots')
        with hold_lock():
            hold = json.loads(Path(RUN_DIR, 'hold.json').read_text())
            hold['worker_requested'] = True
            atomic_json(RUN_DIR + '/hold.json', hold)
        finish_hold()
        # All app, runtime and backup writers are now frozen and all pending
        # filesystem writes drained. This closes the estimate/creation race.
        sources = source_inventory(request['paths'])
        lease = Registry().data.get('leases', {}).get(request['lease'], {})
        if (not lease.get('admitted') or sum(s['bytes'] for s in sources) + SETUP_BYTES
                > min(lease.get('bytes', 0), lease.get('burst_remaining', 0))):
            raise PressureError('snapshot sources grew beyond their reservation; backup postponed')
        backup = runpy.run_path('/usr/bin/reefy-backup', run_name='reefy_snapshot_worker')
        for source in sources:
            if not Writers().frozen():
                raise PressureError('snapshot source barrier was lost')
            name, mount = backup['snapshot_volume'](
                source['path'], request['instance_uuid'], request['timestamp'])
            snapshots.append((name, mount, source['path']))
        request.update(finished=True, snapshots=snapshots)
        atomic_json(REQUEST, request)
    except Exception as error:
        # No release while a timed-out producer may still create an LV. This
        # worker's command waits reap their children; systemd interruption is
        # handled separately by abort_preparation before orphan recovery.
        try:
            cleanup_orphans()
        except Exception:
            pass  # Surviving LVs retain the full durable lease.
        request.update(finished=True, error=f'backup postponed: {type(error).__name__}: {error}')
        atomic_json(REQUEST, request)
        hold = json.loads(Path(RUN_DIR, 'hold.json').read_text())
        if not hold.get('drained') or hold.get('deadline_exceeded'):
            raise
    # This is the same fresh quota verification as explicit storage recovery.
    # The full COW reservation remains charged throughout the Borg transfer.
    recover(preserve_snapshot_worker=True)


if __name__ == '__main__':
    coordinate()
