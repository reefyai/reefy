#!/usr/bin/env python3
"""Pause real protection processes and verify freezer/recovery on test firmware."""
import json
import os
from pathlib import Path
import signal
import shutil
import sys
import time

sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_admission import reservation
from reefy.storage_quota import RUN_DIR, command, physical_sample
from reefy.storage_runtime import LAYER_INITIAL_SIZE
from reefy.storage_watchdog import QUIESCE_SECONDS



def completed_drain():
    """Require the actual systemd drain worker, not just a frozen process."""
    path = Path(RUN_DIR, 'hold.json')
    while True:
        hold = json.loads(path.read_text())
        assert not hold.get('deadline_exceeded'), hold
        assert not hold.get('drain_error'), hold
        if hold.get('drained'):
            assert hold['drained_monotonic'] - hold['monotonic'] <= QUIESCE_SECONDS
            assert hold['worker_requested'] and hold['frozen'], hold
            return {'freeze_seconds': hold['frozen_monotonic'] - hold['monotonic'],
                    'drain_seconds': hold['drained_monotonic'] - hold['frozen_monotonic']}
        assert time.monotonic() - hold['monotonic'] <= QUIESCE_SECONDS, hold
        time.sleep(0.1)


def metadata_fault(counter):
    """Inject an 85% metadata counter only into the real watchdog's sampler.

    The pool remains healthy and unchanged. This is daemon/cgroup fault
    injection, not a claim that the test physically exhausted its metadata LV.
    """
    binary = shutil.which('dmsetup')
    assert binary
    folder = Path('/run/synthetic-metadata-fault')
    folder.mkdir()
    wrapper = folder / 'dmsetup'
    mode = folder / 'mode'
    marker = folder / 'timeout-observed'
    stall = folder / 'stall-observed'
    stall_calls = folder / 'stall-calls'
    mode.write_text('timeout-once')
    hook = folder / 'sitecustomize.py'
    hook.write_text('import pathlib, subprocess, time\n'
        'original_wait = subprocess.Popen.wait\n'
        'mode_path = pathlib.Path(' + repr(str(mode)) + ')\n'
        'stall_path = pathlib.Path(' + repr(str(stall)) + ')\n'
        'def delayed_wait(self, timeout=None):\n'
        "    if (timeout is None and isinstance(self.args, list)\n"
        "            and pathlib.Path(self.args[0]).name == 'dmsetup'\n"
        "            and mode_path.read_text() in ('cleanup-stall', 'cleanup-transient') and not stall_path.exists()):\n"
        "        stall_path.write_text('injected')\n"
        "        time.sleep(.4 if mode_path.read_text() == 'cleanup-transient' else 20)\n"
        '    return original_wait(self, timeout=timeout)\n'
        'subprocess.Popen.wait = delayed_wait\n')
    wrapper.write_text('#!/usr/bin/python3\nimport subprocess, sys, pathlib, time\n'
        'mode = pathlib.Path(' + repr(str(mode)) + ').read_text()\n'
        'marker = pathlib.Path(' + repr(str(marker)) + ')\n'
        'stall = pathlib.Path(' + repr(str(stall)) + ')\n'
        'stall_calls = pathlib.Path(' + repr(str(stall_calls)) + ')\n'
        "if sys.argv[1] == 'status' and mode == 'cleanup-stall':\n"
        "    with stall_calls.open('a') as out: out.write('call\\n')\n"
        "    time.sleep(12)\n"
        "if sys.argv[1] == 'status' and mode == 'cleanup-transient' and not stall.exists():\n"
        "    time.sleep(12)\n"
        "if sys.argv[1] == 'status' and mode == 'timeout-once' and not marker.exists():\n"
        "    marker.write_text('injected'); time.sleep(3)\n"
        'result = subprocess.run(' + repr([binary]) + ' + sys.argv[1:], capture_output=True, text=True)\n'
        'output = result.stdout\n'
        "if result.returncode == 0 and sys.argv[1] == 'status' and mode == 'metadata':\n"
        '    fields = output.split()\n'
        "    index = fields.index('thin-pool') + 2\n"
        "    capacity = int(fields[index].split('/')[1])\n"
        "    fields[index] = str((capacity * 85 + 99) // 100) + '/' + str(capacity)\n"
        "    output = ' '.join(fields) + '\\n'\n"
        'sys.stdout.write(output)\nsys.stderr.write(result.stderr)\nsys.exit(result.returncode)\n')
    wrapper.chmod(0o700)
    dropin = Path('/run/systemd/system/reefy-storage-watchdog.service.d/synthetic-metadata.conf')
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text('[Service]\nEnvironment="PATH=' + str(folder) + ':' + os.environ['PATH'] + '"\n'
                      'Environment="PYTHONPATH=' + str(folder) + ':/usr/lib/reefy"\n'
                      'Environment=PYTHONDONTWRITEBYTECODE=1\n')
    started = time.monotonic()
    try:
        command(['systemctl', 'daemon-reload'])
        command(['systemctl', 'restart', 'reefy-storage-watchdog.service'], timeout=20)
        while not marker.exists():
            assert time.monotonic() - started < 10
            time.sleep(0.1)
        before = counter.stat().st_mtime_ns
        until = time.monotonic() + 6
        while time.monotonic() < until:
            assert not Path(RUN_DIR, 'hold.json').exists(), 'one sampler timeout latched protection'
            time.sleep(0.1)
        assert counter.stat().st_mtime_ns > before
        # Child reaping may finish just after the outer two-second deadline.
        # Discard that expired result and require a new real sample, within the
        # same total four-second retry budget, before allowing writes to continue.
        mode.write_text('cleanup-transient')
        before = counter.stat().st_mtime_ns
        started = time.monotonic()
        while not stall.exists():
            assert time.monotonic() - started < 5
            time.sleep(.1)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            assert not Path(RUN_DIR, 'hold.json').exists(), 'late cleanup consumed the fresh retry'
            time.sleep(.1)
        assert counter.stat().st_mtime_ns > before
        mode.write_text('normal')
        stall.unlink()
        # POSIX subprocess.run waits for child exit after timeout. Inject an
        # twenty-second wait at that exact cleanup boundary, representing a child
        # that cannot exit immediately while in kernel I/O. No kernel change or
        # physical metadata exhaustion is used for this daemon fault test.
        observer_pid = command(['systemctl', 'show', '--property=MainPID', '--value',
                                'reefy-storage-watchdog.service']).strip()
        mode.write_text('cleanup-stall')
        stalled_at = time.monotonic()
        while not stall.exists():
            assert time.monotonic() - stalled_at < 4
            time.sleep(0.1)
        stall_observed = time.monotonic()
        while not Path(RUN_DIR, 'hold.json').exists():
            # The observer can bridge I/O stalls only until its last verified
            # sample is ten seconds old. This is the predeclared detection
            # window, followed by the unchanged thirty-second drain deadline.
            assert time.monotonic() - stalled_at < 11, 'physical evidence exceeded detection budget'
            time.sleep(0.1)
        assert 'protection evidence unavailable' in json.loads(
            Path(RUN_DIR, 'hold.json').read_text())['reason']
        completed_drain()
        stopped = counter.stat().st_mtime_ns
        until = stall_observed + 16
        while time.monotonic() < until:
            assert counter.stat().st_mtime_ns == stopped
            time.sleep(0.1)
        assert stall_calls.read_text().splitlines() == ['call'], 'stuck sample spawned replacements'
        assert command(['systemctl', 'show', '--property=MainPID', '--value',
                        'reefy-storage-watchdog.service']).strip() == observer_pid
        mode.write_text('normal')
        while time.monotonic() - stalled_at < 26:
            assert Path(RUN_DIR, 'hold.json').exists(), 'late sample cleared the latched hold'
            time.sleep(0.1)
        command(['systemctl', 'start', 'reefy-storage-recover.service'], timeout=60)
        deadline = time.monotonic() + 10
        while counter.stat().st_mtime_ns == stopped:
            assert time.monotonic() < deadline
            time.sleep(0.1)
        mode.write_text('metadata')
        started = time.monotonic()
        while not Path(RUN_DIR, 'hold.json').exists():
            assert time.monotonic() - started < 8, 'metadata fault did not hold writers'
            time.sleep(0.1)
        reason = json.loads(Path(RUN_DIR, 'hold.json').read_text())['reason']
        assert reason == 'thin-pool health or metadata pressure', reason
        deadline = time.monotonic() + 4
        while 'frozen 1' not in Path('/sys/fs/cgroup/docker.slice/cgroup.events').read_text():
            assert time.monotonic() < deadline
            time.sleep(0.1)
        frozen = counter.stat().st_mtime_ns
        time.sleep(0.5)
        assert counter.stat().st_mtime_ns == frozen
        drain_result = completed_drain()
        actual = physical_sample()
        assert actual.healthy and actual.metadata_used * 100 < actual.metadata_capacity * 85
    finally:
        dropin.unlink()
        command(['systemctl', 'daemon-reload'])
        command(['systemctl', 'restart', 'reefy-storage-watchdog.service'], timeout=20)
        wrapper.unlink()
        mode.unlink()
        marker.unlink(missing_ok=True)
        stall.unlink(missing_ok=True)
        stall_calls.unlink(missing_ok=True)
        hook.unlink()
        folder.rmdir()
    command(['systemctl', 'start', 'reefy-storage-recover.service'], timeout=60)
    deadline = time.monotonic() + 10
    while counter.stat().st_mtime_ns == frozen:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    return {'one_sampler_timeout_recovered_with_fresh_measurement': True,
            'late_cleanup_retried_with_fresh_measurement_inside_original_budget': True,
            'stuck_command_cleanup_bounded_without_worker_fanout': True,
            'metadata_counter_fault_contained_and_recovered': True,
            'systemd_worker': drain_result}


def run():
    root = '/mnt/reefy-data/apps/synthetic-new/state'
    counter = Path(root) / 'watchdog-counter'
    with reservation('synthetic-watchdog-writer', 2 * LAYER_INITIAL_SIZE):
        command(['docker', 'run', '-d', '--name', 'synthetic-watchdog-writer',
                 '-v', root + ':/state', 'busybox:1.37.0', 'sh', '-c',
                 'i=0; while true; do echo $i > /state/watchdog-counter; i=$((i+1)); sleep 0.1; done'])
    results = {}
    try:
        for unit, limit in [('reefy-storage-guard.service', 25),
                            ('reefy-storage-watchdog.service', 20)]:
            deadline = time.monotonic() + 20
            while not counter.exists():
                assert time.monotonic() < deadline
                time.sleep(0.1)
            before = counter.stat().st_mtime_ns
            time.sleep(0.5)
            assert counter.stat().st_mtime_ns > before
            pid = int(command(['systemctl', 'show', '--property=MainPID', '--value', unit]))
            assert pid > 1
            paused = time.monotonic()
            os.kill(pid, signal.SIGSTOP)
            while not Path(RUN_DIR, 'hold.json').exists():
                assert time.monotonic() - paused < limit, 'protection did not hold allocating writers'
                time.sleep(0.1)
            deadline = time.monotonic() + 4
            while 'frozen 1' not in Path('/sys/fs/cgroup/docker.slice/cgroup.events').read_text():
                assert time.monotonic() < deadline
                time.sleep(0.1)
            frozen = counter.stat().st_mtime_ns
            time.sleep(1)
            assert counter.stat().st_mtime_ns == frozen, 'container kept writing after confirmed freeze'
            for group in ('sshd.service', 'reefy-control.service', 'reefy-mqtt.service'):
                path = Path('/sys/fs/cgroup/system.slice') / group / 'cgroup.freeze'
                if path.exists():
                    assert path.read_text().strip() == '0', 'control or recovery access was frozen'
            results[unit] = {'freeze_seconds': time.monotonic() - paused,
                             'writer_stopped': True, 'systemd_worker': completed_drain()}
            if unit.endswith('watchdog.service'):
                # OnFailure proves containment. Start a healthy replacement
                # before asking the production recovery coordinator to thaw.
                command(['systemctl', 'restart', unit], timeout=20)
            command(['systemctl', 'start', 'reefy-storage-recover.service'], timeout=60)
            assert not Path(RUN_DIR, 'hold.json').exists()
            deadline = time.monotonic() + 10
            while counter.stat().st_mtime_ns == frozen:
                assert time.monotonic() < deadline, 'verified recovery did not resume the writer'
                time.sleep(0.1)
        results['metadata_fault'] = metadata_fault(counter)
        print(json.dumps(results, sort_keys=True))
    finally:
        # If recovery failed, leave the test VM held and preserve the failure
        # instead of manually thawing around the production readiness checks.
        if not Path(RUN_DIR, 'hold.json').exists():
            command(['docker', 'rm', '--force', 'synthetic-watchdog-writer'])


if __name__ == '__main__':
    run()
