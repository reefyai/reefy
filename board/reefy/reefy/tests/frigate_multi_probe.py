#!/usr/bin/env python3
"""Two unmodified Frigate maintainers and an independent committing SQLite app."""
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, '/usr/lib/reefy')
from reefy import shared
from reefy.dataplane import DataPlane
from reefy.storage_admission import reservation
from reefy.storage_quota import Registry, atomic_json, command, physical_sample, state_lock
from reefy.storage_service import ensure_volume
from frigate_storage_probe import IMAGE

NAME = 'synthetic-independent-database'
ROOT = Path('/mnt/reefy-data/apps') / NAME / 'data'
DB_SCRIPT = """import pathlib, sqlite3, time
db = sqlite3.connect('/data/state.db')
db.execute('PRAGMA journal_mode=WAL')
db.execute('CREATE TABLE commits (sequence INTEGER PRIMARY KEY, payload BLOB)')
checkpoint_count = 0
while True:
    db.execute('INSERT INTO commits(payload) VALUES (?)', (b's' * 32768,))
    db.commit()
    sequence = db.execute('SELECT max(sequence) FROM commits').fetchone()[0]
    if sequence % 100 == 0:
        checkpoint = db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        assert checkpoint[0] == 0 and checkpoint[2] >= 0, checkpoint
        checkpoint_count += 1
        pathlib.Path('/data/checkpoints').write_text(str(checkpoint_count))
    pathlib.Path('/data/commits').write_text(str(sequence))
    time.sleep(0.1)
"""


def run():
    pull = '/tmp/synthetic-frigate-pull.json'
    atomic_json(pull, {'services': {'recorder': {'image': IMAGE}}})
    ok, output = DataPlane._run_compose_command(pull, 'synthetic-frigate-pull', ['pull'], 600)
    assert ok, output
    with state_lock():
        path = shared.desired_state_path()
        policy = json.loads(Path(path).read_text())
        policy['app_volumes'].append({'path': str(ROOT)})
        policy['volume_storage_classes'][str(ROOT)] = 'state'
        atomic_json(path, policy)
    ROOT.mkdir(parents=True)
    ensure_volume(str(ROOT), 'state')
    script = Path('/tmp/synthetic-independent-database.py')
    script.write_text(DB_SCRIPT)
    with reservation('synthetic-independent-database', 64 * 1024**2):
        command(['docker', 'run', '-d', '--name', NAME, '--entrypoint', 'python3',
                 '-e', 'PYTHONDONTWRITEBYTECODE=1', '-v', str(ROOT) + ':/data',
                 '-v', str(script) + ':/writer.py:ro', IMAGE, '/writer.py'])
    children, streams, samples = [], [], []
    try:
        for name, bitrate in [('low', 750), ('high', 1250)]:
            stream = open('/tmp/synthetic-frigate-' + name + '.log', 'w')
            streams.append(stream)
            children.append(subprocess.Popen([sys.executable, '/tmp/frigate_storage_probe.py',
                '--name', 'synthetic-frigate-' + name, '--bitrate', str(bitrate), '--skip-pull'],
                stdout=stream, stderr=subprocess.STDOUT))
        started = time.monotonic()
        last_commit = started
        previous_commits = 0
        while any(child.poll() is None for child in children):
            assert time.monotonic() - started < 800, 'Frigate cleanup deadline exceeded'
            count_path = ROOT / 'commits'
            if count_path.exists():
                text = count_path.read_text().strip()
                if text:
                    count = int(text)
                    assert count >= previous_commits
                    if count > previous_commits:
                        last_commit = time.monotonic()
                    previous_commits = count
            assert time.monotonic() - last_commit < 30, 'independent database stopped committing'
            current = physical_sample()
            status = json.loads(Path('/run/reefy/storage-pressure/status.json').read_text())
            assert current.healthy
            assert current.capacity - current.used >= status['allocation']['boundaries']['emergency']
            assert not Path('/run/reefy/storage-pressure/hold.json').exists()
            samples.append({'seconds': time.monotonic() - started, 'used': current.used,
                            'database_commits': previous_commits})
            time.sleep(2)
        for child, stream in zip(children, streams):
            stream.flush()
            log = Path(stream.name).read_text()
            assert child.returncode == 0, log[-20000:]
            assert 'recordings_continue_and_database_integrity' in log, log[-20000:]
            print(log)
        assert previous_commits > 500
        assert int((ROOT / 'checkpoints').read_text()) >= 5
        result = command(['docker', 'exec', NAME, 'python3', '-c',
            "import sqlite3; c=sqlite3.connect('/data/state.db', timeout=30); "
            "assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; "
            "assert c.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()[0]==0; print('ok')"])
        assert result.strip() == 'ok'
        assignments = {row['project'] for row in Registry().data['projects'].values()
                       if row['path'] in (str(ROOT), '/mnt/reefy-data/apps/synthetic-frigate-low/media',
                                          '/mnt/reefy-data/apps/synthetic-frigate-high/media')}
        assert len(assignments) == 3
        print(json.dumps({'two_frigate_maintainers_and_independent_database': 'passed',
                          'database_commits': previous_commits, 'samples': samples}))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        for stream in streams:
            stream.close()
        command(['docker', 'rm', '--force', NAME])


if __name__ == '__main__':
    run()
