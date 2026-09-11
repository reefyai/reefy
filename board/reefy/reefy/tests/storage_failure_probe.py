#!/usr/bin/env python3
"""Pause real protection processes and verify freezer/recovery on test firmware."""
import json
import os
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_admission import reservation
from reefy.storage_quota import RUN_DIR, command
from reefy.storage_runtime import LAYER_INITIAL_SIZE


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
        print(json.dumps(results, sort_keys=True))
    finally:
        # If recovery failed, leave the test VM held and preserve the failure
        # instead of manually thawing around the production readiness checks.
        if not Path(RUN_DIR, 'hold.json').exists():
            command(['docker', 'rm', '--force', 'synthetic-watchdog-writer'])


if __name__ == '__main__':
    run()
