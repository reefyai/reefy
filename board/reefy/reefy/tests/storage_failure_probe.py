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
    wrapper.write_text('#!/usr/bin/python3\nimport subprocess, sys\n'
        'result = subprocess.run(' + repr([binary]) + ' + sys.argv[1:], capture_output=True, text=True)\n'
        'output = result.stdout\n'
        "if result.returncode == 0 and sys.argv[1] == 'status':\n"
        '    fields = output.split()\n'
        "    index = fields.index('thin-pool') + 2\n"
        "    capacity = int(fields[index].split('/')[1])\n"
        "    fields[index] = str((capacity * 85 + 99) // 100) + '/' + str(capacity)\n"
        "    output = ' '.join(fields) + '\\n'\n"
        'sys.stdout.write(output)\nsys.stderr.write(result.stderr)\nsys.exit(result.returncode)\n')
    wrapper.chmod(0o700)
    dropin = Path('/run/systemd/system/reefy-storage-watchdog.service.d/synthetic-metadata.conf')
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text('[Service]\nEnvironment="PATH=' + str(folder) + ':' + os.environ['PATH'] + '"\n')
    started = time.monotonic()
    try:
        command(['systemctl', 'daemon-reload'])
        command(['systemctl', 'restart', 'reefy-storage-watchdog.service'], timeout=20)
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
        actual = physical_sample()
        assert actual.healthy and actual.metadata_used * 100 < actual.metadata_capacity * 85
    finally:
        dropin.unlink()
        command(['systemctl', 'daemon-reload'])
        command(['systemctl', 'restart', 'reefy-storage-watchdog.service'], timeout=20)
        wrapper.unlink()
        folder.rmdir()
    command(['systemctl', 'start', 'reefy-storage-recover.service'], timeout=60)
    deadline = time.monotonic() + 10
    while counter.stat().st_mtime_ns == frozen:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    return {'metadata_counter_fault_contained_and_recovered': True}


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
            results[unit] = {'freeze_seconds': time.monotonic() - paused, 'writer_stopped': True}
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
