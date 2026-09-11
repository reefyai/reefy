#!/usr/bin/env python3
"""Sparse allocation amplification with real dm-thin counters and the watchdog."""
from dataclasses import asdict
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_guard import Guard
from reefy.storage_quota import Registry, RUN_DIR, command, read_quotas, set_quota
from reefy.storage_service import INITIAL_RATE, RESPONSE_SECONDS, IN_FLIGHT
from reefy.storage_watchdog import Writers, check
from thin_storage_probe import ROOT, VG, MIB, CHUNK, sample

GROUP = 'synthetic-storage-sparse'
CGROUP = Path('/sys/fs/cgroup') / GROUP
FILE = Path(ROOT, 'media', 'sparse-pressure')
STATUS = '/run/synthetic-sparse-status.json'
RESULT = Path('/tmp/synthetic-sparse-writer.json')


def writer():
    (CGROUP / 'cgroup.procs').write_text(str(os.getpid()))
    written = 0
    error_number = None
    try:
        with FILE.open('wb', buffering=0) as stream:
            # Pre-size without allocation so writes land inside sparse holes.
            # EOF writes let XFS speculative preallocation charge intervening
            # blocks to the quota and do not reproduce amplification.
            stream.truncate(16 * 1024**3)
            for offset in range(0, 16 * 1024**3, CHUNK):
                stream.seek(offset)
                stream.write(b's' * 4096)
                written += 4096
                # At most 64 partially dirty thin chunks per flush, matching
                # the declared 64 MiB in-flight bound without a rate sleep.
                if written % (64 * 4096) == 0:
                    os.fsync(stream.fileno())
            os.fsync(stream.fileno())
    except OSError as error:
        error_number = error.errno
    RESULT.write_text(json.dumps({'written': written, 'errno': error_number}))


def run():
    assert not Registry().data.get('active'), 'disposable pre-activation VM required'
    assert not Path(RUN_DIR, 'hold.json').exists()
    mounted = json.loads(command(['findmnt', '--json', '--target', ROOT,
                                  '-o', 'TARGET,SOURCE']))['filesystems'][0]
    assert mounted['target'] == ROOT
    assert os.path.realpath(mounted['source']) == os.path.realpath('/dev/' + VG + '/data')
    registry = Registry('/run/reefy/storage-pressure/synthetic-thin-registry.json')
    media = next(row for row in registry.data['projects'].values() if row['path'] == ROOT + '/media')
    guard = Guard(peak_bytes_per_second=INITIAL_RATE, response_seconds=RESPONSE_SECONDS,
                  in_flight_bytes=IN_FLIGHT, registry_path=registry.path,
                  sample=sample, status_path=STATUS)
    registry.data.pop('peak_bytes_per_second', None)
    registry.save()
    initial = guard.pass_once()
    assert not initial['allocation']['quiesce']
    quota = read_quotas(ROOT)[media['project']]
    assert quota['hard'] >= 128 * MIB
    set_quota(ROOT, media['project'], 128 * MIB)
    CGROUP.mkdir()
    writers = Writers(groups=(GROUP,))
    child = subprocess.Popen([sys.executable, __file__, 'writer'])
    trace = []
    started = time.monotonic()
    reason = None
    try:
        while time.monotonic() - started < 19:
            current = sample()
            trace.append({'seconds': time.monotonic() - started, **asdict(current)})
            assert current.healthy
            assert current.capacity - current.used >= initial['allocation']['boundaries']['emergency'], trace[-1]
            reason = check(active=True, stale_seconds=20, sample=sample,
                           writers=writers, status_path=STATUS)
            if reason or child.poll() is not None:
                break
            time.sleep(1)
        if reason:
            assert reason == 'physical emergency boundary reached', reason
            assert 'frozen 1' in (CGROUP / 'cgroup.events').read_text()
        else:
            assert child.poll() == 0 and RESULT.exists(), 'writer escaped bounded observation'
            assert json.loads(RESULT.read_text())['errno'] in (errno.ENOSPC, errno.EDQUOT)
        time.sleep(1)
        # Writer is already contained; allow queued I/O to settle for this
        # final evidence sample. The watchdog deadline above stays unchanged.
        final = sample(timeout=10)
        quota_after = read_quotas(ROOT)[media['project']]['used']
        assert final.healthy
        assert final.capacity - final.used >= initial['allocation']['boundaries']['emergency'], asdict(final)
        assert final.used > initial['sample']['used'] + 128 * MIB, (asdict(final), quota_after)
        assert quota_after <= 128 * MIB
        print(json.dumps({'sparse_allocation_amplification_contained': 'passed',
                          'outcome': reason or 'application allocation error',
                          'physical_growth_bytes': final.used - initial['sample']['used'],
                          'quota_used_bytes': quota_after, 'sample': asdict(final)}))
    finally:
        Path('/tmp/synthetic-sparse-trace.json').write_text(json.dumps({'initial': initial, 'samples': trace}))
        if child.poll() is None:
            child.send_signal(signal.SIGKILL)
        writers.thaw()
        child.wait(timeout=10)
        FILE.unlink(missing_ok=True)
        command(['fstrim', ROOT], timeout=60)
        CGROUP.rmdir()
        Path(RUN_DIR, 'hold.json').unlink(missing_ok=True)


if __name__ == '__main__':
    (writer if sys.argv[1:] == ['writer'] else run)()
