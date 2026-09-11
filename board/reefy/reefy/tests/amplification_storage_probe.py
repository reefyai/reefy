#!/usr/bin/env python3
"""Sparse allocation amplification with real dm-thin counters and the watchdog."""
from dataclasses import asdict
import errno
import json
import mmap
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
from reefy import storage_watchdog
from reefy.storage_watchdog import Writers, check, QUIESCE_SECONDS, finish_hold
from thin_storage_probe import ROOT, VG, MIB, CHUNK, sample, settle_empty_fixture

GROUP = 'synthetic-storage-sparse'
CGROUP = Path('/sys/fs/cgroup') / GROUP
FILE = Path(ROOT, 'media', 'sparse-pressure')
STATUS = '/run/synthetic-sparse-status.json'
RESULT = Path('/tmp/synthetic-sparse-writer.json')


def writer():
    (CGROUP / 'cgroup.procs').write_text(str(os.getpid()))
    written = 0
    error_number = None
    descriptor = None
    try:
        descriptor = os.open(FILE, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_DIRECT, 0o600)
        os.ftruncate(descriptor, 16 * 1024**3)
        # Direct I/O prevents delayed-allocation extent sizing from filling
        # the intervening holes. mmap supplies page-aligned memory for O_DIRECT.
        with mmap.mmap(-1, 4096) as block:
            block[:] = b's' * 4096
            for offset in range(0, 16 * 1024**3, CHUNK):
                assert os.pwrite(descriptor, block, offset) == 4096
                written += 4096
                if written % (64 * 4096) == 0:
                    os.fsync(descriptor)
            os.fsync(descriptor)
    except OSError as error:
        error_number = error.errno
    finally:
        if descriptor is not None:
            os.close(descriptor)
    RESULT.write_text(json.dumps({'written': written, 'errno': error_number}))


def drain():
    finish_hold(writers=Writers(groups=(GROUP,)), mounts=[ROOT])


def guard_loop():
    guard = Guard(peak_bytes_per_second=INITIAL_RATE, response_seconds=RESPONSE_SECONDS,
                  in_flight_bytes=IN_FLIGHT,
                  registry_path=RUN_DIR + '/synthetic-thin-registry.json',
                  sample=sample, status_path=STATUS)
    while True:
        guard.pass_once()
        time.sleep(10)


def run():
    assert not Registry().data.get('active'), 'disposable pre-activation VM required'
    assert not Path(RUN_DIR, 'hold.json').exists()
    mounted = json.loads(command(['findmnt', '--json', '--target', ROOT,
                                  '-o', 'TARGET,SOURCE']))['filesystems'][0]
    assert mounted['target'] == ROOT
    assert os.path.realpath(mounted['source']) == os.path.realpath('/dev/' + VG + '/data')
    Path(ROOT, 'media', 'cow').unlink(missing_ok=True)
    settle_empty_fixture()
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
    media['max_hard'] = 128 * MIB
    registry.save()
    # Slow guest I/O need not reach the physical boundary within one stale
    # heartbeat window. Run the actual guard independently at its production
    # cadence while the observer keeps its existing one-second checks.
    controller = subprocess.Popen([sys.executable, __file__, 'guard'])
    CGROUP.mkdir()
    writers = Writers(groups=(GROUP,))
    child = subprocess.Popen([sys.executable, __file__, 'writer'])
    drainer = None
    trace = []
    started = time.monotonic()
    reason = None
    def observed_sample(*, timeout):
        current = sample(timeout=timeout)
        trace.append({'seconds': time.monotonic() - started, **asdict(current)})
        return current

    storage_watchdog.physical_sample = observed_sample
    try:
        while time.monotonic() - started < 90:
            check_started = time.monotonic()
            reason = check(active=True, stale_seconds=20,
                           writers=writers, status_path=STATUS)
            if reason or child.poll() is not None:
                assert time.monotonic() - check_started <= 4.5
                break
            time.sleep(1)
        if reason:
            assert reason in ('physical emergency boundary reached',
                              'protection evidence unavailable: TimeoutExpired',
                              'protection evidence unavailable: TimeoutError',
                              'guard measurements are stale'), reason
            drainer = subprocess.Popen([sys.executable, __file__, 'drain'], start_new_session=True)
            hold = json.loads(Path(RUN_DIR, 'hold.json').read_text())
            while not hold.get('drained'):
                assert not hold.get('drain_error'), hold
                assert time.monotonic() - hold['monotonic'] <= QUIESCE_SECONDS
                time.sleep(1)
                tick = time.monotonic()
                check(active=True, stale_seconds=20, writers=writers, status_path=STATUS)
                assert time.monotonic() - tick <= 4.5, 'observer blocked on pending freeze'
                hold = json.loads(Path(RUN_DIR, 'hold.json').read_text())
            assert not hold.get('deadline_exceeded'), hold
            assert 'frozen 1' in (CGROUP / 'cgroup.events').read_text()
        else:
            assert child.poll() == 0 and RESULT.exists(), 'writer escaped bounded observation'
            assert json.loads(RESULT.read_text())['errno'] in (errno.ENOSPC, errno.EDQUOT)
        if drainer is None:
            command([sys.executable, '-c',
                     'import sys; sys.path.insert(0, "/usr/lib/reefy"); '
                     'from reefy.storage_quota import flush_filesystem; '
                     'flush_filesystem(sys.argv[1])', ROOT], timeout=QUIESCE_SECONDS)
        else:
            assert drainer.wait(timeout=2) == 0
        # Writer is already contained; allow queued I/O to settle for this
        # final evidence sample. The watchdog deadline above stays unchanged.
        final = sample(timeout=10)
        quota_after = read_quotas(ROOT)[media['project']]['used']
        assert all(row['healthy'] and row['capacity'] - row['used'] >=
                   initial['allocation']['boundaries']['emergency'] for row in trace), trace
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
        controller.terminate()
        controller.wait(timeout=15)
        if child.poll() is None:
            child.send_signal(signal.SIGKILL)
        if drainer is not None:
            if drainer.poll() is None:
                os.killpg(drainer.pid, signal.SIGKILL)
            drainer.wait(timeout=5)
        writers.thaw()
        child.wait(timeout=10)
        FILE.unlink(missing_ok=True)
        command(['fstrim', ROOT], timeout=60)
        CGROUP.rmdir()
        Path(RUN_DIR, 'hold.json').unlink(missing_ok=True)
        registry = Registry(registry.path)
        for row in registry.data['projects'].values():
            if row['path'] == ROOT + '/media':
                row.pop('max_hard', None)
        registry.save()


if __name__ == '__main__':
    {'writer': writer, 'drain': drain, 'guard': guard_loop}.get(
        sys.argv[1] if len(sys.argv) > 1 else '', run)()
