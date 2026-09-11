#!/usr/bin/env python3
"""Build legacy synthetic storage, then verify automatic boot migration."""
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, '/usr/lib/reefy')
from reefy import shared
from reefy.storage import Storage
from reefy.storage_quota import (Registry, STATE_DIR, atomic_json, command, mount_info,
                                 read_quotas, require_enforcement, state_lock, verify_tree)

ROOT = '/mnt/reefy-data/apps/synthetic-scale/media'
EVIDENCE = STATE_DIR + '/synthetic-scale.json'


def prepare():
    storage = Storage()
    assert storage._ensure_volume_lv(ROOT)
    mount = mount_info(ROOT)
    command(['umount', ROOT])
    # This fixture represents a pre-quota XFS filesystem. Enabling accounting,
    # assigning projects and opening Docker are all production boot actions.
    command(['mount', '-o', 'noatime,noquota', mount['source'], ROOT])
    started = time.monotonic()
    for directory in range(1000):
        path = Path(ROOT) / f'd{directory:04d}'
        path.mkdir()
        for number in range(1000):
            with (path / f'f{number:04d}').open('wb') as stream:
                if number in (0, 999):
                    stream.write(f'synthetic-{directory}-{number}'.encode())
    big = Path(ROOT) / 'recording-sized'
    with big.open('wb', buffering=0) as stream:
        for _ in range(32):
            stream.write(b'r' * 1024**2)
        os.fsync(stream.fileno())
    os.link(big, Path(ROOT) / 'hardlink')
    (Path(ROOT) / 'outside-link').symlink_to('/mnt/reefy-data/apps/synthetic-new/state')
    metadata = {key: getattr(big.stat(), key) for key in ('st_size', 'st_uid', 'st_gid', 'st_mtime_ns')}
    command(['sync'], timeout=60)
    policy_path = shared.desired_state_path()
    policy = json.loads(Path(policy_path).read_text())
    policy['app_volumes'].append({'path': ROOT})
    policy['volume_storage_classes'][ROOT] = 'bulk'
    fresh = '/mnt/reefy-data/apps/synthetic-offline-new/config'
    policy['app_volumes'].append({'path': fresh})
    policy['volume_storage_classes'][fresh] = 'state'
    policy.setdefault('backup', {}).setdefault('instances', []).append({'paths': [fresh]})
    assert storage._volume_lv_name(fresh) not in storage._lv_metadata_names()
    atomic_json(policy_path, policy)
    assert not any(row['path'] == ROOT for row in Registry().data['projects'].values())
    evidence = {'create_seconds': time.monotonic() - started, 'metadata': metadata,
                'previous_boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    with state_lock():
        registry = Registry()
        registry.data.setdefault('leases', {})['synthetic-interrupted-operation'] = {
            'kind': 'synthetic-interrupted-operation', 'bytes': 4096,
            'storage_class': 'runtime', 'target': None, 'pid': os.getpid(),
            'boot_id': evidence['previous_boot'],
        }
        registry.save()
    atomic_json(EVIDENCE, evidence)
    print(json.dumps(evidence))


def verify():
    evidence = json.loads(Path(EVIDENCE).read_text())
    assert evidence['previous_boot'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    registry = Registry()
    assert registry.data['active'] and registry.data['inventory_complete']
    assert not registry.data.get('activation_pending')
    assert 'synthetic-interrupted-operation' not in registry.data.get('leases', {})
    assert not Path('/run/reefy/storage-pressure/hold.json').exists()
    row = next(row for row in registry.data['projects'].values() if row['path'] == ROOT)
    fresh = '/mnt/reefy-data/apps/synthetic-offline-new/config'
    assert Storage()._dedicated_volume_mount_status(fresh) is True
    new_record = next(row for row in registry.data['projects'].values() if row['path'] == fresh)
    assert new_record['complete']
    verify_tree(fresh, new_record['project'])
    require_enforcement(ROOT)
    assert row['complete'] and row['inodes'] >= 1001000
    assert verify_tree(ROOT, row['project']) == row['inodes']
    for directory in range(1000):
        for number in (0, 999):
            assert (Path(ROOT) / f'd{directory:04d}' / f'f{number:04d}').read_text() == f'synthetic-{directory}-{number}'
    big = Path(ROOT) / 'recording-sized'
    assert {key: getattr(big.stat(), key) for key in evidence['metadata']} == evidence['metadata']
    assert big.stat().st_ino == (Path(ROOT) / 'hardlink').stat().st_ino
    assert (Path(ROOT) / 'outside-link').is_symlink()
    quota = read_quotas(ROOT)[row['project']]
    assert quota['used'] >= 32 * 1024**2 and quota['hard'] > 0
    assert int(command(['systemctl', 'show', '--property=MainPID', '--value', 'docker.service'])) > 1
    print(json.dumps({'million_inode_automatic_migration': 'passed', 'inodes': row['inodes'],
                      'project': row['project'], 'quota': quota, 'fixture': evidence}, sort_keys=True))


if __name__ == '__main__':
    {'prepare': prepare, 'verify': verify}[sys.argv[1]]()
