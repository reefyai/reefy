#!/usr/bin/env python3
"""Reject the formerly unsafe 4 GiB COW workload before creating a snapshot.

Uses the same dedicated 16 GiB fixture and production admission calculation.
The raw COW/freezer probes remain separate diagnostics with their own evidence.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_guard import Guard
from reefy.storage_quota import Registry, RUN_DIR, command, flush_filesystem, set_quota
from reefy.storage_service import INITIAL_RATE, RESPONSE_SECONDS, IN_FLIGHT
from reefy import storage_snapshot_admission as snapshot
from thin_storage_probe import ROOT, VG, sample, write_file, settle_empty_fixture


def run():
    assert not Registry().data.get('active'), 'dedicated pre-activation guest required'
    assert not Path(RUN_DIR, 'hold.json').exists()
    mounted = json.loads(command(['findmnt', '--json', '--target', ROOT,
                                  '-o', 'TARGET,SOURCE']))['filesystems'][0]
    device = '/dev/' + VG + '/data'
    assert mounted['target'] == ROOT
    assert os.path.realpath(mounted['source']) == os.path.realpath(device)
    registry = Registry(RUN_DIR + '/synthetic-thin-registry.json')
    for row in registry.data['projects'].values():
        Path(row['path'], 'pressure-data').unlink(missing_ok=True)
    Path(ROOT, 'media', 'cow').unlink(missing_ok=True)
    settle_empty_fixture()
    media = next(row for row in registry.data['projects'].values()
                 if row['path'] == ROOT + '/media')
    fixture = Path(ROOT, 'media', 'cow-pressure')
    set_quota(ROOT, media['project'], 5 * 1024**3)
    write_file(fixture, 4 * 1024**3)
    flush_filesystem(ROOT)
    registry.data.pop('peak_bytes_per_second', None)
    registry.save()
    guard = Guard(peak_bytes_per_second=INITIAL_RATE, response_seconds=RESPONSE_SECONDS,
                  in_flight_bytes=IN_FLIGHT, registry_path=registry.path,
                  sample=sample, status_path='/run/synthetic-cow-admission.json')
    initial = guard.pass_once()
    assert not initial['allocation']['quiesce'], initial
    # Select only the verified disposable pool. Production's mapped-sector
    # reader and reservation sizing remain unchanged; its app-path inventory
    # is replaced because this fixture intentionally has one shared test LV.
    snapshot.STORAGE_VG, snapshot.STORAGE_POOL = VG, 'pool'
    mapped = snapshot.mapped_bytes(device)
    assert mapped >= 4 * 1024**3
    snapshot.source_inventory = lambda paths: [
        {'path': ROOT, 'device': device, 'bytes': mapped, 'storage_class': 'bulk'}]
    budget, storage_class = snapshot.reservation_size([ROOT])
    before_lvs = command(['lvs', '--noheadings', '-o', 'lv_name', VG]).split()
    sentinel = Path(ROOT, 'state', 'admission-counter')
    child = subprocess.Popen([sys.executable, '-c',
        'import pathlib,time; p=pathlib.Path(' + repr(str(sentinel)) + '); '
        '\nwhile True: p.write_text(str(time.monotonic_ns())); time.sleep(.05)'])
    try:
        deadline = time.monotonic() + 5
        while not sentinel.exists():
            assert time.monotonic() < deadline
            time.sleep(.05)
        before = sentinel.read_text()
        registry = Registry(registry.path)
        registry.data['leases'] = {'synthetic-full-cow': {
            'kind': 'backup-snapshots', 'bytes': budget, 'target': None,
            'storage_class': storage_class, 'admitted': False,
            'burst_remaining': budget}}
        registry.save()
        result = guard.pass_once()
        assert 'synthetic-full-cow' not in result['admitted_leases'], result
        assert not Registry(registry.path).data['leases']['synthetic-full-cow']['admitted']
        assert not result['allocation']['quiesce'], result
        assert not Path(RUN_DIR, 'hold.json').exists()
        time.sleep(.2)
        assert child.poll() is None and sentinel.read_text() != before
        assert command(['lvs', '--noheadings', '-o', 'lv_name', VG]).split() == before_lvs
        current = sample()
        assert current.healthy
        assert current.capacity - current.used >= result['allocation']['boundaries']['emergency']
        evidence = {'full_cow_admission_denied_before_snapshot': 'passed',
                    'independent_state_progress': 'passed', 'source_mapped_bytes': mapped,
                    'reservation_bytes': budget, 'sample': asdict(current),
                    'allocation': result['allocation']}
        Path('/tmp/synthetic-cow-trace.json').write_text(json.dumps(evidence))
        print(json.dumps(evidence))
    finally:
        child.terminate()
        child.wait(timeout=5)
        sentinel.unlink(missing_ok=True)
        registry = Registry(registry.path)
        registry.data['leases'] = {}
        registry.save()
        fixture.unlink()
        settle_empty_fixture()


if __name__ == '__main__':
    run()
