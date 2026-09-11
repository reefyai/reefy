#!/usr/bin/env python3
"""Real snapshot COW and the independent freezer on the disposable thin pool.

All writer processes and block devices belong to this test. This measures the
QEMU response envelope; it does not certify a faster physical deployment disk.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_guard import Guard
from reefy.storage_quota import Registry, RUN_DIR, command, flush_filesystem, read_quotas, set_quota
from reefy.storage_service import INITIAL_RATE, RESPONSE_SECONDS, IN_FLIGHT
from reefy.storage_watchdog import Writers, check
from thin_storage_probe import ROOT, VG, MIB, CHUNK, sample, write_file

GROUP = 'synthetic-storage-cow'
CGROUP = Path('/sys/fs/cgroup') / GROUP
FILE = Path(ROOT, 'media', 'cow-pressure')
STATUS = '/run/synthetic-cow-status.json'


def writer():
    (CGROUP / 'cgroup.procs').write_text(str(os.getpid()))
    with FILE.open('r+b', buffering=0) as stream:
        while True:
            for offset in range(0, 4 * 1024**3, CHUNK):
                stream.seek(offset)
                stream.write(b'c' * 4096)
            os.fsync(stream.fileno())


def run():
    assert not Registry().data.get('active'), 'requires a dedicated pre-activation test VM'
    assert not Path(RUN_DIR, 'hold.json').exists()
    mounted = json.loads(command(['findmnt', '--json', '--target', ROOT,
                                  '-o', 'TARGET,SOURCE']))['filesystems'][0]
    assert mounted['target'] == ROOT
    assert os.path.realpath(mounted['source']) == os.path.realpath('/dev/' + VG + '/data')
    registry = Registry('/run/reefy/storage-pressure/synthetic-thin-registry.json')
    for row in registry.data['projects'].values():
        Path(row['path'], 'pressure-data').unlink(missing_ok=True)
    Path(ROOT, 'media', 'cow').unlink(missing_ok=True)
    command(['fstrim', ROOT], timeout=60)
    media = next(row for row in registry.data['projects'].values() if row['path'] == ROOT + '/media')
    # Bounded fixture preparation with no snapshot or app writer present.
    # The actual containment phase starts with fresh production measurements.
    set_quota(ROOT, media['project'], 5 * 1024**3)
    write_file(FILE, 4 * 1024**3)
    flush_filesystem(ROOT)
    registry.data.pop('peak_bytes_per_second', None)
    registry.save()
    guard = Guard(peak_bytes_per_second=INITIAL_RATE, response_seconds=RESPONSE_SECONDS,
                  in_flight_bytes=IN_FLIGHT, registry_path=registry.path,
                  sample=sample, status_path=STATUS)
    initial = guard.pass_once()
    assert not initial['allocation']['quiesce'], initial
    assert sample().used < initial['physical_stop_bytes']
    quota_before = read_quotas(ROOT)[media['project']]['used']
    command(['lvcreate', '--snapshot', '--setactivationskip', 'n', '-n', 'pressure_hold', VG + '/data'])
    CGROUP.mkdir()
    writers = Writers(groups=(GROUP,))
    child = subprocess.Popen([sys.executable, __file__, 'writer'])
    trace = []
    started = time.monotonic()
    crossed = None
    try:
        reason = None
        while time.monotonic() - started < 19:
            current = sample()
            elapsed = time.monotonic() - started
            trace.append({'seconds': elapsed, **asdict(current)})
            if current.used >= initial['physical_stop_bytes'] and crossed is None:
                crossed = time.monotonic()
            reason = check(active=True, stale_seconds=20, sample=sample,
                           writers=writers, status_path=STATUS)
            if reason:
                break
            time.sleep(1)  # same independent observer cadence as firmware
        assert reason == 'physical emergency boundary reached', (reason, initial, trace)
        assert 'frozen 1' in (CGROUP / 'cgroup.events').read_text()
        assert crossed is not None
        assert time.monotonic() - crossed <= 4, 'freezer exceeded its declared response bound'
        # The predeclared emergency reserve must still exist after queued I/O
        # settles; quota usage stays flat while physical COW grows separately.
        time.sleep(1)
        after = sample()
        assert after.healthy
        assert after.capacity - after.used >= initial['allocation']['boundaries']['emergency'], asdict(after)
        assert abs(read_quotas(ROOT)[media['project']]['used'] - quota_before) <= 4 * MIB
        assert after.used > initial['sample']['used'] + 128 * MIB
        print(json.dumps({'snapshot_cow_independent_containment': 'passed',
                          'freeze_seconds': time.monotonic() - started,
                          'physical_growth_bytes': after.used - initial['sample']['used'],
                          'sample': asdict(after)}))
    finally:
        Path('/tmp/synthetic-cow-trace.json').write_text(json.dumps({'initial': initial, 'samples': trace}))
        # Test-only cleanup, after preserving containment evidence. Never use
        # this manual freezer release as the production recovery path.
        child.send_signal(signal.SIGKILL)
        writers.thaw()
        child.wait(timeout=10)
        command(['lvremove', '-f', VG + '/pressure_hold'])
        FILE.unlink()
        command(['fstrim', ROOT], timeout=60)
        CGROUP.rmdir()
        Path(RUN_DIR, 'hold.json').unlink(missing_ok=True)


if __name__ == '__main__':
    (writer if sys.argv[1:] == ['writer'] else run)()
