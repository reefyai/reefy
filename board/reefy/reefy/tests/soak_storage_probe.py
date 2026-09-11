#!/usr/bin/env python3
"""Manual five-hour mixed-workload gate after the two-recorder correctness test."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import time
sys.path.insert(0, '/usr/lib/reefy')
from reefy.dataplane import DataPlane
from reefy.storage_admission import reservation
from reefy.storage_quota import Registry, RUN_DIR, command, physical_sample
from frigate_multi_probe import DB_SCRIPT, ROOT
from frigate_storage_probe import IMAGE


def run(seconds):
    assert 60 <= seconds <= 5 * 3600
    assert Registry().data.get('active')
    assert not Path(RUN_DIR, 'hold.json').exists()
    database = 'synthetic-soak-database'
    source = json.loads(command(['findmnt', '--json', '--target', '/mnt/reefy-data',
                                '-o', 'SOURCE']))['filesystems'][0]['source']
    # The runner owns this disposable VM; resolve the exact mounted LV rather
    # than guessing a live-device disk from size or ordinal.
    lv = json.loads(command(['lvs', '--reportformat', 'json', '-o', 'vg_name,lv_name', source]))
    row = lv['report'][0]['lv'][0]
    target = row['vg_name'].strip() + '/' + row['lv_name'].strip()
    snapshot = row['vg_name'].strip() + '/synthetic_soak_hold'
    names = ('synthetic-frigate-low', 'synthetic-frigate-high')
    previous = int((ROOT / 'commits').read_text())
    script = Path('/tmp/synthetic-soak-database.py')
    script.write_text(DB_SCRIPT.replace('CREATE TABLE commits', 'CREATE TABLE IF NOT EXISTS commits'))
    with reservation('synthetic-soak-database', 64 * 1024**2):
        command(['docker', 'run', '-d', '--name', database, '--entrypoint', 'python3',
                 '-e', 'PYTHONDONTWRITEBYTECODE=1', '-v', str(ROOT) + ':/data',
                 '-v', str(script) + ':/writer.py:ro', IMAGE, '/writer.py'])
    traces, cleanup_seen = [], set()
    lease, snapshot_started = None, None
    started = last_progress = last_snapshot = time.monotonic()
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
                command(['lvremove', '-f', snapshot], timeout=30)
                snapshot_started = None
                lease.close()
                lease = None
                command(['fstrim', '/mnt/reefy-data'], timeout=60)
            elif snapshot_started is None and now - last_snapshot >= 600:
                lease = ExitStack()
                lease.enter_context(reservation('synthetic-soak-snapshot', 64 * 1024**2))
                command(['lvcreate', '--snapshot', '--setactivationskip', 'n',
                         '-n', 'synthetic_soak_hold', target], timeout=30)
                snapshot_started = last_snapshot = now
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
        result = command(['docker', 'exec', database, 'python3', '-c',
            "import sqlite3; c=sqlite3.connect('/data/state.db'); "
            "assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; "
            "assert c.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()[0]==0; print('ok')"])
        assert result.strip() == 'ok'
        print(json.dumps({'mixed_storage_soak': 'passed', 'seconds': seconds,
                          'cleanup_instances': sorted(cleanup_seen), 'database_commits': previous}))
    finally:
        Path('/tmp/synthetic-soak-trace.json').write_text(json.dumps(traces))
        # On a hold, preserve the stopped VM for diagnosis. Normal completion
        # releases only this scenario's snapshot and containers.
        if not Path(RUN_DIR, 'hold.json').exists():
            if snapshot_started is not None:
                command(['lvremove', '-f', snapshot], timeout=30)
            if lease:
                lease.close()
            command(['docker', 'rm', '--force', database])
            for name in names:
                DataPlane._run_compose_command('/tmp/' + name + '.json', name, ['down'], 120)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=5 * 3600)
    run(parser.parse_args().seconds)
