#!/usr/bin/env python3
"""Exports consume media quota even when few recordings can be reclaimed."""
import errno
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_admission import reservation, wait_generation
from reefy.storage_quota import Registry, command, read_quotas, physical_sample, state_lock
from frigate_storage_probe import IMAGE, ROOT, MIB

SCRIPT = r"""import io, json, logging, threading
from types import SimpleNamespace
from peewee import SqliteDatabase
from frigate.models import Event, Recordings
from frigate.storage import StorageMaintainer
stream = io.StringIO()
handler = logging.StreamHandler(stream)
logger = logging.getLogger('frigate.storage')
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)
db = SqliteDatabase('/config/frigate.db', timeout=30)
db.bind([Event, Recordings])
maintainer = StorageMaintainer(SimpleNamespace(cameras={'synthetic': SimpleNamespace()}), threading.Event())
maintainer.calculate_camera_bandwidth()
assert 0 < Recordings.select().count() < 100
assert maintainer.check_storage_needs_cleanup()
maintainer.reduce_storage_consumption()
assert 'Could not clear' in stream.getvalue(), stream.getvalue()
assert 'Cleaned up' not in stream.getvalue(), stream.getvalue()
assert Recordings.select().count() == 0
assert maintainer.check_storage_needs_cleanup(), 'exports were incorrectly treated as reclaimable recordings'
assert db.execute_sql('PRAGMA integrity_check').fetchone()[0] == 'ok'
db.close()
print(json.dumps({'unmodified_cleanup_cannot_reclaim_exports': 'passed'}))
"""


def run():
    media = ROOT / 'media'
    with state_lock():
        registry = Registry()
        key, row = next((key, row) for key, row in registry.data['projects'].items()
                        if row['path'] == str(media))
        previous = row['max_hard']
        used = read_quotas(row['mount'])[row['project']]['used']
        row['max_hard'] = ((used + 16 * MIB + 4095) // 4096) * 4096
        registry.data['generation'] = registry.data.get('generation', 0) + 1
        generation = registry.data['generation']
        registry.save()
    wait_generation(generation, volume=key)
    export = media / 'exports/synthetic-export'
    export.parent.mkdir(exist_ok=True)
    number = None
    try:
        with export.open('wb', buffering=0) as stream:
            for _ in range(32):
                try:
                    stream.write(b'e' * MIB)
                    os.fsync(stream.fileno())
                except OSError as error:
                    number = error.errno
                    break
        assert number in (errno.ENOSPC, errno.EDQUOT), number
        before = export.stat().st_size
        assert before > MIB
        script = Path('/tmp/synthetic-frigate-exports.py')
        script.write_text(SCRIPT)
        with reservation('synthetic-frigate-export-check', 64 * MIB):
            result = command(['docker', 'run', '--rm', '--name', 'synthetic-frigate-exports',
                '--entrypoint', 'python3', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                '-e', 'PYTHONPATH=/opt/frigate',
                '-v', str(ROOT / 'config') + ':/config', '-v', str(media) + ':/media/frigate',
                '-v', str(script) + ':/check.py:ro', IMAGE, '/check.py'], timeout=60)
        assert export.stat().st_size == before
        with export.open('rb') as stream:
            assert stream.read(4096) == b'e' * 4096
        (ROOT / 'config/state-after-export-pressure').write_text('state remains writable')
        assert physical_sample().healthy
        assert not Path('/run/reefy/storage-pressure/hold.json').exists()
        print(result)
    finally:
        export.unlink(missing_ok=True)
        with state_lock():
            registry = Registry()
            registry.data['projects'][key]['max_hard'] = previous
            registry.data['generation'] = registry.data.get('generation', 0) + 1
            generation = registry.data['generation']
            registry.save()
        wait_generation(generation, volume=key)


if __name__ == '__main__':
    run()
