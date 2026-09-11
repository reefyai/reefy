#!/usr/bin/env python3
"""Pause and resume real project tagging under held-snapshot physical pressure."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_migration import Migration, MIGRATION_IN_FLIGHT
from reefy.storage_pressure import PressureError, boundaries
from reefy.storage_quota import Registry, command, flush_filesystem, read_quotas, set_quota, verify_tree
from thin_storage_probe import ROOT, VG, MIB, sample


def run():
    assert not Registry().data.get('active'), 'disposable pre-activation VM required'
    registry = Registry('/run/reefy/storage-pressure/synthetic-thin-registry.json')
    root_record = next(r for r in registry.data['projects'].values() if r['path'] == ROOT)
    set_quota(ROOT, root_record['project'], 11 * 1024**3)
    tree = Path(ROOT, 'migration-cow')
    filler = Path(ROOT, 'migration-fill')
    tree.mkdir()
    for directory in range(100):
        parent = tree / f'd{directory:03d}'
        parent.mkdir()
        for number in range(1000):
            (parent / f'f{number:04d}').write_bytes(b'preserved' if number == 0 else b'')
    flush_filesystem(ROOT)
    command(['fstrim', ROOT], timeout=60)
    limits = boundaries(sample().capacity)
    # Only this test-owned pool is filled. Keep the production base emergency
    # reserve and start tagging 16 MiB below its migration pressure boundary.
    stop = limits.state - MIGRATION_IN_FLIGHT
    target = stop - 16 * MIB
    block = b'f' * MIB
    with filler.open('wb', buffering=0) as stream:
        while True:
            current = sample(timeout=10)
            count = min(128, max(0, (target - current.used) // MIB))
            if not count:
                break
            for _ in range(count):
                stream.write(block)
            os.fsync(stream.fileno())
    flush_filesystem(ROOT)
    before = sample(timeout=10)
    assert before.used < stop
    snapshot_created = False
    reports = []
    try:
        command(['lvcreate', '--snapshot', '--setactivationskip', 'n', '-n',
                 'migration_hold', VG + '/data'])
        snapshot_created = True
        worker = Migration(registry=registry, sample=lambda: sample(timeout=10), report=reports.append)
        try:
            worker.prepare({str(tree): 'bulk'})
        except PressureError as error:
            assert 'migration paused' in str(error), error
        else:
            raise AssertionError('tagging did not reach the physical migration boundary')
        after = sample(timeout=10)
        row = next(r for r in registry.data['projects'].values() if r['path'] == str(tree))
        project = row['project']
        assert not row.get('complete')
        assert read_quotas(ROOT)[project]['used'] > 0, 'no files were retagged before pause'
        assert after.used > before.used and after.used >= stop, (asdict(before), asdict(after))
        assert after.healthy and after.capacity - after.used >= limits.emergency
        # The production migration margin must cover this batch before the
        # emergency reserve is touched. Never widen it to make the test pass.
        command(['lvremove', '-f', VG + '/migration_hold'])
        snapshot_created = False
        filler.unlink()
        command(['fstrim', ROOT], timeout=60)
        recovered = sample(timeout=10)
        assert recovered.used < before.used
        worker.prepare({str(tree): 'bulk'})
        assert row['project'] == project and row['complete']
        assert verify_tree(str(tree), project) == 100101
        for directory in range(100):
            assert (tree / f'd{directory:03d}' / 'f0000').read_bytes() == b'preserved'
        print(json.dumps({'snapshot_metadata_migration_pause_and_resume': 'passed',
                          'before': asdict(before), 'paused': asdict(after),
                          'recovered': asdict(recovered), 'project': project,
                          'progress': reports}))
    finally:
        if snapshot_created:
            command(['lvremove', '-f', VG + '/migration_hold'])
        filler.unlink(missing_ok=True)
        shutil.rmtree(tree)
        command(['fstrim', ROOT], timeout=60)


if __name__ == '__main__':
    run()
