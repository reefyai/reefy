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
from reefy import storage_watchdog
from reefy.storage_watchdog import Writers, check, FREEZE_SECONDS, DRAIN_SECONDS
from thin_storage_probe import ROOT, VG, MIB, CHUNK, SERIAL, sample, write_file

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
    if os.environ.get('REEFY_COW_DISABLE_WBT') == '1':
        devices = json.loads(command(['lsblk', '--json', '--nodeps', '-o', 'PATH,SERIAL']))['blockdevices']
        selected = [Path(d['path']).name for d in devices if d.get('serial') == SERIAL]
        assert len(selected) == 1
        (Path('/sys/class/block') / selected[0] / 'queue/wbt_lat_usec').write_text('0')
    initial['memory_before_writer'] = Path('/proc/meminfo').read_text()
    writeback = None
    if os.environ.get('REEFY_COW_LIMIT_DIRTY') == '1':
        dev = os.stat(ROOT).st_dev
        writeback = Path('/sys/class/bdi') / f'{os.major(dev)}:{os.minor(dev)}'
        (writeback / 'max_bytes').write_text(str(MIB))
        (writeback / 'strict_limit').write_text('1')
        assert 0 < int((writeback / 'max_bytes').read_text()) <= MIB
    CGROUP.mkdir()
    writers = Writers(groups=(GROUP,))
    child = subprocess.Popen([sys.executable, __file__, 'writer'])
    trace = []
    observer_calls = []
    started = time.monotonic()
    crossed = None
    def observed_sample(*, timeout):
        nonlocal crossed
        current = sample(timeout=timeout)
        trace.append({'seconds': time.monotonic() - started, **asdict(current)})
        if current.used >= initial['physical_stop_bytes'] and crossed is None:
            crossed = time.monotonic()
        return current

    # Select the disposable pool without changing the production retry/check
    # implementation. Diagnostic sampling must never delay the actual observer.
    storage_watchdog.physical_sample = observed_sample
    try:
        reason = None
        while time.monotonic() - started < 19:
            check_started = time.monotonic()
            reason = check(active=True, stale_seconds=20,
                           writers=writers, status_path=STATUS)
            observer_calls.append(time.monotonic() - check_started)
            assert observer_calls[-1] <= 4.5, 'observer exceeded its sample budget'
            if reason:
                assert time.monotonic() - check_started <= 4.5, 'observer blocked on pending freeze'
                break
            time.sleep(1)  # same independent observer cadence as firmware
        assert reason in ('physical emergency boundary reached',
                          'protection evidence unavailable: TimeoutExpired',
                          'protection evidence unavailable: TimeoutError'), (reason, initial, trace)
        hold = json.loads(Path(RUN_DIR, 'hold.json').read_text())
        while not hold.get('frozen'):
            assert time.monotonic() - hold['monotonic'] <= FREEZE_SECONDS
            time.sleep(1)
            tick = time.monotonic()
            check(active=True, stale_seconds=20, writers=writers, status_path=STATUS)
            observer_calls.append(time.monotonic() - tick)
            assert observer_calls[-1] <= 4.5, 'observer blocked on pending freeze'
            hold = json.loads(Path(RUN_DIR, 'hold.json').read_text())
        assert not hold.get('deadline_exceeded'), hold
        assert 'frozen 1' in (CGROUP / 'cgroup.events').read_text()
        confirmed = time.monotonic()
        if crossed is not None:
            assert time.monotonic() - crossed <= FREEZE_SECONDS + 5, 'freezer exceeded its declared response bound'
        else:
            assert reason.startswith('protection evidence unavailable:'), reason
        # The predeclared emergency reserve must still exist after queued I/O
        # settles; quota usage stays flat while physical COW grows separately.
        drain_started = time.monotonic()
        command([sys.executable, '-c',
                 'import sys; from reefy.storage_quota import flush_filesystem; '
                 'flush_filesystem(sys.argv[1])', ROOT], timeout=DRAIN_SECONDS)
        drain_seconds = time.monotonic() - drain_started
        # Writer is already contained; allow queued I/O to settle for this
        # final evidence sample. The watchdog deadline above stays unchanged.
        after = sample(timeout=10)
        assert all(row['healthy'] and row['capacity'] - row['used'] >=
                   initial['allocation']['boundaries']['emergency'] for row in trace), trace
        assert after.healthy
        assert after.capacity - after.used >= initial['allocation']['boundaries']['emergency'], asdict(after)
        assert abs(read_quotas(ROOT)[media['project']]['used'] - quota_before) <= 4 * MIB
        assert after.used > initial['sample']['used'] + 128 * MIB
        print(json.dumps({'snapshot_cow_independent_containment': 'passed',
                          'total_scenario_seconds': time.monotonic() - started,
                          'freeze_seconds': confirmed - hold['monotonic'],
                          'drain_seconds': drain_seconds,
                          'observer_max_call_seconds': max(observer_calls),
                          'response_budget_seconds': RESPONSE_SECONDS,
                          'containment_reason': reason,
                          'writeback_limit_experiment': bool(writeback),
                          'physical_growth_bytes': after.used - initial['sample']['used'],
                          'sample': asdict(after)}))
    except Exception:
        print(Path('/proc/meminfo').read_text(), file=sys.stderr)
        for p in Path('/sys/class/bdi').glob('*/*'):
            if p.name in ('max_bytes', 'strict_limit', 'read_ahead_kb'):
                try:
                    print(str(p) + ': ' + p.read_text().strip(), file=sys.stderr)
                except OSError:
                    pass
        # Preserve blocked kernel-task evidence before test cleanup thaws the
        # writer. A repeated dmsetup timeout is not a successful containment.
        for process in Path('/proc').iterdir():
            if not process.name.isdigit():
                continue
            try:
                status = (process / 'status').read_text()
                state = next(line for line in status.splitlines() if line.startswith('State:'))
                name = next(line for line in status.splitlines() if line.startswith('Name:'))
                if any(value in state for value in ('D (', 'T (')) or process.name == str(child.pid):
                    print(json.dumps({'pid': process.name, 'name': name, 'state': state,
                          'wchan': (process / 'wchan').read_text(),
                          'stack': (process / 'stack').read_text(),
                          'cgroup': (process / 'cgroup').read_text()}), file=sys.stderr)
            except (OSError, StopIteration):
                pass
        Path('/proc/sysrq-trigger').write_text('w')
        if os.environ.get('REEFY_COW_MEASURE_DRAIN') == '1':
            # Diagnostic only: leave the original freeze request in place and
            # measure its eventual completion. The failed deadline above still
            # fails this run, even if I/O later drains with space remaining.
            drain_started = time.monotonic()
            events = ''
            while time.monotonic() - drain_started < 60:
                events = (CGROUP / 'cgroup.events').read_text()
                if 'frozen 1' in events:
                    break
                time.sleep(1)
            evidence = {'diagnostic_drain_seconds': time.monotonic() - drain_started,
                        'events': events, 'memory': Path('/proc/meminfo').read_text()}
            try:
                evidence['physical'] = asdict(sample(timeout=10))
            except Exception as error:
                evidence['sample_error'] = type(error).__name__
            print(json.dumps(evidence), file=sys.stderr, flush=True)
        raise
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
