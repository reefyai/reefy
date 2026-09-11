#!/usr/bin/env python3
"""Real SQLite allocation failure and restart on a governed state volume."""
import json
from pathlib import Path
import sys

sys.path.insert(0, '/usr/lib/reefy')
from reefy import shared
from reefy.storage_admission import reservation, wait_generation
from reefy.storage_quota import Registry, atomic_json, command, physical_sample, state_lock
from reefy.storage_service import ensure_volume
from frigate_storage_probe import IMAGE

ROOT = Path('/mnt/reefy-data/apps/synthetic-full-database/data')
NAME = 'synthetic-full-database'
MIB = 1024**2
SCRIPT = r"""import json, pathlib, sqlite3, time
path = pathlib.Path('/data/state.db')
existing = path.exists()
db = sqlite3.connect(str(path))
db.execute('PRAGMA journal_mode=WAL')
if existing:
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    before = db.execute('SELECT count(*) FROM commits').fetchone()[0]
    db.execute('INSERT INTO commits(payload) VALUES (?)', (b'resumed',))
    db.commit()
    assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    print(json.dumps({'restart_integrity': 'ok', 'preserved_commits': before,
                      'resumed_commits': db.execute('SELECT count(*) FROM commits').fetchone()[0]}), flush=True)
else:
    db.execute('CREATE TABLE commits (sequence INTEGER PRIMARY KEY, payload BLOB)')
    db.commit()
    confirmed = 0
    try:
        while True:
            db.execute('INSERT INTO commits(payload) VALUES (?)', (b's' * 131072,))
            db.commit()
            confirmed += 1
            time.sleep(0.005)
    except sqlite3.OperationalError as error:
        assert error.sqlite_errorcode == sqlite3.SQLITE_FULL, error
        db.rollback()
        assert confirmed > 10
        print(json.dumps({'allocation_error': 'SQLITE_FULL', 'committed': confirmed}), flush=True)
    finally:
        db.close()
"""


def quota_cap(size):
    with state_lock():
        registry = Registry()
        key, row = next((key, row) for key, row in registry.data['projects'].items()
                        if row['path'] == str(ROOT))
        row['max_hard'] = size
        registry.data['generation'] = registry.data.get('generation', 0) + 1
        generation = registry.data['generation']
        registry.save()
    wait_generation(generation, volume=key)


def run():
    # The preceding Frigate gate pulled this exact image through normal admission.
    command(['docker', 'image', 'inspect', IMAGE])
    with state_lock():
        path = shared.desired_state_path()
        policy = json.loads(Path(path).read_text())
        policy['app_volumes'].append({'path': str(ROOT)})
        policy['volume_storage_classes'][str(ROOT)] = 'state'
        atomic_json(path, policy)
    ROOT.mkdir(parents=True)
    ensure_volume(str(ROOT), 'state')
    quota_cap(64 * MIB)
    script = Path('/tmp/synthetic-full-database.py')
    script.write_text(SCRIPT)
    with reservation('synthetic-database-create', 64 * MIB):
        output = command(['docker', 'run', '--name', NAME, '--entrypoint', 'python3',
                          '-e', 'PYTHONDONTWRITEBYTECODE=1', '-v', str(ROOT) + ':/data',
                          '-v', str(script) + ':/writer.py:ro', IMAGE, '/writer.py'], timeout=90)
    full = json.loads(output)
    assert full['allocation_error'] == 'SQLITE_FULL'
    quota_cap(128 * MIB)
    output = command(['docker', 'start', '--attach', NAME], timeout=60)
    restored = json.loads(output)
    assert restored['preserved_commits'] == full['committed'], (full, restored)
    assert restored['resumed_commits'] == full['committed'] + 1
    current = physical_sample()
    status = json.loads(Path('/run/reefy/storage-pressure/status.json').read_text())
    assert current.healthy
    assert current.capacity - current.used >= status['allocation']['boundaries']['emergency']
    assert not Path('/run/reefy/storage-pressure/hold.json').exists()
    command(['docker', 'rm', NAME])
    print(json.dumps({'sqlite_full_and_committed_data_after_restart': 'passed',
                      **full, **restored}))


if __name__ == '__main__':
    run()
