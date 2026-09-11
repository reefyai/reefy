#!/usr/bin/env python3
"""Manual five-hour mixed-workload gate after the two-recorder correctness test."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import runpy
import sys
import time
sys.path.insert(0, '/usr/lib/reefy')
from reefy.dataplane import DataPlane
from reefy import shared
from reefy.storage import Storage
from reefy.storage_admission import reservation
from reefy.storage_pressure import PressureError
from reefy.storage_quota import Registry, RUN_DIR, command, physical_sample, atomic_json, state_lock
from reefy.storage_snapshot_admission import reservation_size, create_reserved
from reefy.storage_snapshots import snapshots_released
from frigate_multi_probe import DB_SCRIPT
from frigate_storage_probe import IMAGE

ROOT = Path('/mnt/reefy-data/apps/synthetic-soak-database/data')


def run(seconds):
    assert 60 <= seconds <= 5 * 3600
    assert Registry().data.get('active')
    assert not Path(RUN_DIR, 'hold.json').exists()
    database = 'synthetic-soak-database'
    # A fresh dedicated app LV exercises the production snapshot coordinator.
    # Do not create a raw default-LV snapshot with an undersized synthetic lease.
    assert not ROOT.exists(), 'soak requires a fresh disposable app instance'
    with state_lock():
        policy_path = shared.desired_state_path()
        policy = json.loads(Path(policy_path).read_text())
        policy['app_volumes'].append({'path': str(ROOT)})
        policy['volume_storage_classes'][str(ROOT)] = 'state'
        atomic_json(policy_path, policy)
    storage = Storage()
    storage.set_storage_classes({str(ROOT): 'state'})
    storage._prepare_app_dirs([{'path': str(ROOT), 'uid': 0}], {str(ROOT)})
    (ROOT / 'commits').write_text('0')
    release_snapshot = runpy.run_path('/usr/bin/reefy-backup')['release_snapshot']
    names = ('synthetic-frigate-low', 'synthetic-frigate-high')
    previous = int((ROOT / 'commits').read_text())
    script = Path('/tmp/synthetic-soak-database.py')
    script.write_text(DB_SCRIPT.replace('CREATE TABLE commits', 'CREATE TABLE IF NOT EXISTS commits'))
    with reservation('synthetic-soak-database', 64 * 1024**2):
        command(['docker', 'run', '-d', '--name', database, '--entrypoint', 'python3',
                 '-e', 'PYTHONDONTWRITEBYTECODE=1', '-v', str(ROOT) + ':/data',
                 '-v', str(script) + ':/writer.py:ro', IMAGE, '/writer.py'])
    traces, cleanup_seen = [], set()
    lease, snapshot_started, created = None, None, []
    snapshots_created = snapshots_postponed = 0
    started = last_progress = time.monotonic()
    next_snapshot = started + 30
    last_recording_check = started
    try:
        for name in names:
            ok, output = DataPlane._run_compose_command('/tmp/' + name + '.json', name,
                                                       ['up', '-d', '--pull', 'never'], 180)
            assert ok, output
        while time.monotonic() - started < seconds:
            now = time.monotonic()
            text = (ROOT / 'commits').read_text().strip()
            count = int(text) if text else previous
            assert count >= previous
            if count > previous:
                last_progress = now
            previous = count
            assert now - last_progress < 30, 'independent database stopped committing'
            current = physical_sample()
            status = json.loads(Path(RUN_DIR, 'status.json').read_text())
            assert current.healthy
            assert current.capacity - current.used >= status['allocation']['boundaries']['emergency']
            assert current.metadata_used * 100 < current.metadata_capacity * 85
            assert not Path(RUN_DIR, 'hold.json').exists()
            pid = int(command(['systemctl', 'show', '--property=MainPID', '--value',
                               'reefy-storage-guard.service']).strip())
            process = Path('/proc') / str(pid)
            stat = (process / 'stat').read_text().split(') ', 1)[1].split()
            memory = next(line for line in (process / 'status').read_text().splitlines()
                          if line.startswith('VmRSS:')).split()[1]
            traces.append({'seconds': now - started, 'physical_used': current.used,
                           'metadata_used': current.metadata_used,
                           'emergency': status['allocation']['boundaries']['emergency'],
                           'stage': status['allocation']['stage'], 'database_commits': count,
                           'guard_cpu_ticks': int(stat[11]) + int(stat[12]),
                           'guard_rss_kib': int(memory)})
            if snapshot_started is not None and now - snapshot_started >= 30:
                for name, mount, _ in created:
                    assert release_snapshot(name, mount)
                created = []
                snapshot_started = None
                lease.close()
                lease = None
                command(['fstrim', str(ROOT)], timeout=60)
            elif snapshot_started is None and now >= next_snapshot:
                next_snapshot = now + 600
                budget, storage_class = reservation_size([str(ROOT)])
                lease = ExitStack()
                try:
                    identity = lease.enter_context(reservation('backup-snapshots', budget,
                        storage_class=storage_class, burst_bytes=budget,
                        release_check=snapshots_released))
                except PressureError as error:
                    assert str(error) in ('operation exceeds its storage class ceiling',
                                          'physical capacity cannot admit this operation'), str(error)
                    assert not Path(RUN_DIR, 'hold.json').exists()
                    assert snapshots_released()
                    lease.close()
                    lease = None
                    snapshots_postponed += 1
                else:
                    created = create_reserved([str(ROOT)], database, int(time.time()), identity)
                    assert len(created) == 1
                    snapshot_started = time.monotonic()
                    snapshots_created += 1
                    # Snapshot setup includes a deliberate writer barrier.
                    # Resume the progress deadline after that completed barrier.
                    last_progress = snapshot_started
            if now - last_recording_check >= 300:
                for name in names:
                    cid = command(['docker', 'ps', '-q', '--filter',
                                   'label=com.docker.compose.project=' + name]).strip()
                    assert cid
                    recent = Path('/mnt/reefy-data/apps', name, 'media/recordings').rglob('*.mp4')
                    assert max(path.stat().st_mtime for path in recent) > time.time() - 120
                    logs = command(['docker', 'logs', '--since', '6m', cid], timeout=20)
                    if 'Cleaned up' in logs:
                        cleanup_seen.add(name)
                last_recording_check = now
                # Durable partial evidence survives a later assertion failure.
                Path('/tmp/synthetic-soak-trace.json').write_text(json.dumps(traces))
                print(json.dumps({'soak_seconds': int(now - started), 'database_commits': count}), flush=True)
            time.sleep(5)
        if seconds >= 1800:
            assert cleanup_seen == set(names), cleanup_seen
        assert snapshots_created > 0, 'soak never exercised an admitted snapshot'
        result = command(['docker', 'exec', database, 'python3', '-c',
            "import sqlite3; c=sqlite3.connect('/data/state.db'); "
            "assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; "
            "assert c.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()[0]==0; print('ok')"])
        assert result.strip() == 'ok'
        print(json.dumps({'mixed_storage_soak': 'passed', 'seconds': seconds,
                          'cleanup_instances': sorted(cleanup_seen), 'database_commits': previous,
                          'snapshots_created': snapshots_created,
                          'snapshots_postponed': snapshots_postponed}))
    finally:
        Path('/tmp/synthetic-soak-trace.json').write_text(json.dumps(traces))
        # On a hold, preserve the stopped VM for diagnosis. Normal completion
        # releases only this scenario's snapshot and containers.
        if not Path(RUN_DIR, 'hold.json').exists():
            for name, mount, _ in created:
                assert release_snapshot(name, mount)
            if lease:
                lease.close()
            command(['docker', 'rm', '--force', database])
            for name in names:
                DataPlane._run_compose_command('/tmp/' + name + '.json', name, ['down'], 120)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=5 * 3600)
    run(parser.parse_args().seconds)
