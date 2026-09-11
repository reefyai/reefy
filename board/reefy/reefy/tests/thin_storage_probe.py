#!/usr/bin/env python3
"""Real dm-thin pressure, COW and discard tests on a dedicated QEMU test disk.

No live device pool is selected by size or ordinal. The harness must attach a
blank disk with the exact synthetic serial below; otherwise this probe fails.
"""
from dataclasses import asdict
import errno
import json
import os
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_guard import Guard
from reefy.storage_migration import Migration
from reefy.storage_pressure import GB
from reefy.storage_quota import (Registry, command, flush_filesystem, physical_sample,
                                 read_quotas, set_quota)


SERIAL = 'reefy-storage-e2e-pool'
VG = 'quota_probe'
POOL = VG + '-pool-tpool'
ROOT = '/mnt/quota-probe'
CHUNK = 512 * 1024
MIB = 1024**2


def sample():
    return physical_sample(POOL, timeout=2)


def records(registry):
    return {row['path']: row for row in registry.data['projects'].values()}


def write_file(path, size):
    with open(path, 'wb', buffering=0) as stream:
        block = b's' * MIB
        for _ in range(size // MIB):
            stream.write(block)
        os.fsync(stream.fileno())


def run():
    devices = json.loads(command(['lsblk', '--json', '--nodeps', '-o', 'PATH,SERIAL']))['blockdevices']
    targets = [device['path'] for device in devices if device.get('serial') == SERIAL]
    assert len(targets) == 1, 'dedicated synthetic test disk missing'
    device = targets[0]
    signatures = json.loads(command(['wipefs', '--json', device])).get('signatures', [])
    assert not signatures, 'test disk must be blank'
    command(['pvcreate', '-y', device])
    command(['vgcreate', VG, device])
    command(['lvcreate', '--type', 'thin-pool', '-L', '12G', '--poolmetadatasize', '128M',
             '--chunksize', '512K', '-Zn', '-n', 'pool', VG], timeout=60)
    command(['lvcreate', '--thin', '--virtualsize', '12G', '-n', 'data', VG + '/pool'])
    command(['mkfs.xfs', '-q', '/dev/' + VG + '/data'], timeout=60)
    Path(ROOT).mkdir()
    command(['mount', '-o', 'pquota,discard', '/dev/' + VG + '/data', ROOT])
    results = {'versions': {'kernel': command(['uname', '-r']).strip(),
                           'xfs': command(['xfs_quota', '-V']).strip(),
                           'lvm': command(['lvm', 'version']).splitlines()[0]},
               'initial_sample': asdict(sample())}
    paths = {ROOT: 'runtime', ROOT + '/media': 'bulk', ROOT + '/state': 'state'}
    for path in paths:
        Path(path).mkdir(exist_ok=True)
    registry = Registry('/run/reefy/storage-pressure/synthetic-thin-registry.json')
    Migration(registry=registry, sample=sample).prepare(paths)
    registry.data.update(active=True, inventory_complete=True)
    registry.save()
    guard = Guard(peak_bytes_per_second=128 * MIB, response_seconds=4,
                  in_flight_bytes=64 * MIB, registry_path=registry.path,
                  sample=sample, status_path='/run/synthetic-thin-status.json')
    initial = guard.pass_once()
    rows = records(registry)
    media, state = rows[ROOT + '/media'], rows[ROOT + '/state']
    assert initial['sample']['healthy']

    # Real quota behavior at a pressure transition, without fabricating pool
    # counters. Fill only the granted media allowance and require allocation
    # errors to stay local while an independent state write succeeds.
    start = sample()
    error_number = None
    with open(ROOT + '/media/recordings', 'wb', buffering=0) as stream:
        for _ in range(5000):
            try:
                stream.write(b'x' * MIB)
            except OSError as error:
                error_number = error.errno
                break
        os.fsync(stream.fileno())
    assert error_number in (errno.ENOSPC, errno.EDQUOT), error_number
    Path(ROOT + '/state/committed').write_text('durable state')
    flush_filesystem(ROOT)
    after = sample()
    assert after.healthy and after.capacity - after.used >= initial['allocation']['boundaries']['emergency']
    assert after.used > start.used
    results['local_quota_errors_and_independent_state'] = 'passed'
    Path(ROOT + '/media/recordings').unlink()
    command(['fstrim', ROOT], timeout=60)
    # A fresh guard avoids treating the synthetic fast setup workload as a
    # qualification of the long-running production rate envelope.
    guard.previous_time = None
    guard.pass_once()

    # Disable discard: logical deletion must not be counted as physical credit.
    command(['mount', '-o', 'remount,nodiscard', ROOT])
    write_file(ROOT + '/media/no-discard', 512 * MIB)
    before_delete = sample()
    used_before = read_quotas(ROOT)[media['project']]['used']
    Path(ROOT + '/media/no-discard').unlink()
    flush_filesystem(ROOT)
    after_delete = sample()
    used_after = read_quotas(ROOT)[media['project']]['used']
    assert used_after < used_before
    assert after_delete.used >= before_delete.used - 4 * CHUNK
    limited = guard.pass_once()
    command(['fstrim', ROOT], timeout=60)
    after_trim = sample()
    assert after_trim.used < after_delete.used - 256 * MIB
    recovered = guard.pass_once()
    assert recovered['allocation']['granted'] > limited['allocation']['granted']
    results['delayed_discard_uses_real_physical_credit'] = 'passed'
    command(['mount', '-o', 'remount,discard', ROOT])

    # A held thin snapshot can allocate 512 MiB while project usage stays flat.
    write_file(ROOT + '/media/cow', 512 * MIB)
    flush_filesystem(ROOT)
    quota_before = read_quotas(ROOT)[media['project']]['used']
    command(['lvcreate', '--snapshot', '--setactivationskip', 'n', '-n', 'snapshot', VG + '/data'])
    before_cow = sample()
    with open(ROOT + '/media/cow', 'r+b', buffering=0) as stream:
        for offset in range(0, 512 * MIB, CHUNK):
            stream.seek(offset)
            stream.write(b'c' * 4096)
        os.fsync(stream.fileno())
    flush_filesystem(ROOT)
    after_cow = sample()
    quota_after = read_quotas(ROOT)[media['project']]['used']
    assert abs(quota_after - quota_before) < 4 * MIB
    assert after_cow.used - before_cow.used >= 400 * MIB
    assert after_cow.healthy
    command(['lvremove', '-f', VG + '/snapshot'])
    assert sample().used < after_cow.used - 256 * MIB
    results['snapshot_cow_is_visible_outside_logical_usage'] = 'passed'
    results['final_sample'] = asdict(sample())
    print(json.dumps(results, sort_keys=True))


if __name__ == '__main__':
    run()
