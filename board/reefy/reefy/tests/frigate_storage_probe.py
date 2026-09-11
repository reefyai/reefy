#!/usr/bin/env python3
"""Pinned, unmodified Frigate recording/cleanup loop on a QEMU test device.

A private test ceiling keeps the runtime short. The production guard still
owns all quotas. This checks application integration, not physical rate bounds.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time

sys.path.insert(0, '/usr/lib/reefy')
from reefy import shared
from reefy.dataplane import DataPlane
from reefy.storage_admission import reservation, wait_generation
from reefy.storage_quota import (Registry, atomic_json, command, read_quotas,
                                 physical_sample, state_lock, verify_tree)
from reefy.storage_service import ensure_volume

IMAGE = 'ghcr.io/blakeblackshear/frigate:0.17.0'
NAME = 'synthetic-frigate'
ROOT = Path('/mnt/reefy-data/apps/synthetic-frigate')
COMPOSE = '/tmp/synthetic-frigate.json'
MIB = 1024**2
BITRATE = 1000
SKIP_PULL = False


def compose_run(args, timeout=180):
    ok, output = DataPlane._run_compose_command(COMPOSE, NAME, args, timeout)
    assert ok, output
    return output


def container():
    return command(['docker', 'ps', '-aq', '--filter',
                    'label=com.docker.compose.project=' + NAME]).strip()



# Firmware Python intentionally omits SQLite. Run all SQL against the real
# database using the pinned image's SQLite library through a local Unix socket.
SQL_SERVER = r"""import json, socketserver, sqlite3
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline())
        connection = sqlite3.connect('/config/frigate.db', timeout=30)
        try:
            result = connection.execute(request['sql'], request.get('parameters', [])).fetchall()
            connection.commit()
            response = {'rows': result}
        except Exception as error:
            response = {'error': str(error)}
        finally:
            connection.close()
        self.wfile.write(json.dumps(response).encode() + b'\n')
socketserver.UnixStreamServer('/config/synthetic-sql.sock', Handler).serve_forever()
"""


def start_sql_helper():
    script = Path('/tmp/' + NAME + '-sql-server.py')
    script.write_text(SQL_SERVER)
    with reservation('synthetic-sql-helper', 64 * MIB):
        command(['docker', 'run', '-d', '--name', NAME + '-sql',
                 '--entrypoint', 'python3', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                 '-v', str(ROOT / 'config') + ':/config',
                 '-v', str(script) + ':/sql-server.py:ro', IMAGE, '/sql-server.py'])
    deadline = time.monotonic() + 30
    while not (ROOT / 'config/synthetic-sql.sock').exists():
        assert time.monotonic() < deadline, 'SQLite helper did not start'
        time.sleep(0.1)


def sql(statement, parameters=()):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(40)
        client.connect(str(ROOT / 'config/synthetic-sql.sock'))
        client.sendall(json.dumps({'sql': statement, 'parameters': parameters}).encode() + b'\n')
        with client.makefile('rb') as stream:
            response = json.loads(stream.readline())
    if 'error' in response:
        raise RuntimeError(response['error'])
    return response['rows']


def wait_recording(db, after, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            row = sql('SELECT max(start_time) FROM recordings')[0]
            if row[0] is not None and row[0] > after:
                return row[0]
        except RuntimeError:
            pass
        time.sleep(2)
    raise AssertionError('Frigate did not persist a new recording segment')


def run():
    policy_path = shared.desired_state_path()
    paths = {str(ROOT / 'config'): 'state', str(ROOT / 'media'): 'bulk',
             str(ROOT / 'cache'): 'bulk'}
    with state_lock():
        policy = json.loads(Path(policy_path).read_text())
        for path, storage_class in paths.items():
            policy['app_volumes'].append({'path': path})
            policy['volume_storage_classes'][path] = storage_class
        atomic_json(policy_path, policy)
    for path, storage_class in paths.items():
        Path(path).mkdir(parents=True)
        ensure_volume(path, storage_class)
    config = {'mqtt': {'enabled': False}, 'auth': {'enabled': False},
              'telemetry': {'version_check': False},
              'detectors': {'cpu': {'type': 'cpu', 'num_threads': 1}},
              'detect': {'enabled': False},
              'record': {'enabled': True, 'continuous': {'days': 3}, 'motion': {'days': 3}},
              'logger': {'default': 'info', 'logs': {'frigate.storage': 'debug'}},
              'ffmpeg': {'output_args': {'record': 'preset-record-generic'}},
              'cameras': {'synthetic': {'ffmpeg': {'inputs': [
                  {'path': '/config/source.mp4', 'input_args': ['-re', '-stream_loop', '-1'],
                   'roles': ['record']} ]}, 'detect': {'width': 320, 'height': 240, 'fps': 5}}}}
    atomic_json(str(ROOT / 'config/config.yml'), config)
    service = {'image': IMAGE, 'shm_size': '256m', 'restart': 'no',
               'volumes': [str(ROOT / 'config') + ':/config',
                           str(ROOT / 'media') + ':/media/frigate',
                           str(ROOT / 'cache') + ':/tmp/cache'],
               'logging': {'driver': 'json-file', 'options': {'max-size': '20m', 'max-file': '3'}}}
    atomic_json(COMPOSE, {'services': {'recorder': service}})
    if not SKIP_PULL:
        compose_run(['pull'], 600)
    # Generate an actual decodable constant-bitrate clip using the image's own
    # ffmpeg. No external cameras, test monkeypatches or copied customer data.
    generator = dict(service)
    generator.update(entrypoint='/bin/sh', command=['-c',
        'ffmpeg_bin=$$(find /usr/lib/ffmpeg -name ffmpeg -type f | head -n 1); '
        'exec "$$ffmpeg_bin" -hide_banner -loglevel error -y -f lavfi '
        '-i testsrc2=size=320x240:rate=5 -t 10 -an -c:v libx264 -threads 1 '
        f'-b:v {BITRATE}k -minrate {BITRATE}k -maxrate {BITRATE}k -bufsize {2 * BITRATE}k '
        '-x264-params nal-hrd=cbr -pix_fmt yuv420p /config/source.mp4'])
    atomic_json(COMPOSE, {'services': {'recorder': generator}})
    compose_run(['up', '--abort-on-container-exit', '--exit-code-from', 'recorder', '--pull', 'never'], 180)
    clip = ROOT / 'config/source.mp4'
    assert clip.stat().st_size > 512 * 1024
    atomic_json(COMPOSE, {'services': {'recorder': service}})
    start_sql_helper()
    first_start = time.time()
    compose_run(['up', '-d', '--force-recreate', '--pull', 'never'])
    db = ROOT / 'config/frigate.db'
    wait_recording(db, first_start)
    compose_run(['stop'])
    clip_size = clip.stat().st_size
    seed_paths = []
    now = time.time()
    media_key = next(key for key, row in Registry().data['projects'].items()
                     if row['path'] == str(ROOT / 'media'))
    seed_budget = ((clip_size * 380 + 4095) // 4096) * 4096
    with reservation('synthetic-recording-history', seed_budget, storage_class='bulk', target=media_key):
        for index in range(370):
            seed_started = time.monotonic()
            # The regular age-retention worker keeps these recent recordings;
            # only the free-space maintainer should remove this history.
            start = now - (370 - index) * 10 - 120
            stamp = time.gmtime(start)
            relative = Path('recordings') / time.strftime('%Y-%m-%d/%H', stamp) / 'synthetic' / time.strftime('%M.%S.mp4', stamp)
            path = ROOT / 'media' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(clip, path)
            seed_paths.append(path)
            sql('INSERT INTO recordings '
                '(id, camera, path, start_time, end_time, duration, segment_size) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (f'synthetic-seed-{index}', 'synthetic', '/media/frigate/' + str(relative),
                 start, start + 10, 10, clip_size / MIB))
            # This fixture models recording history, not an unbounded disk-rate
            # qualification. Two seeders together stay below the declared
            # physical rate envelope; sparse/COW probes exercise fast divergence.
            time.sleep(max(0, clip_size / (24 * MIB) - (time.monotonic() - seed_started)))
        assert sql('PRAGMA integrity_check')[0][0] == 'ok'
    # Set only an internal test cap. The ordinary allocator applies it, so a
    # background pass cannot silently replace the experiment's limit.
    with state_lock():
        registry = Registry()
        key, media = next((key, row) for key, row in registry.data['projects'].items()
                          if row['path'] == str(ROOT / 'media'))
        media['max_hard'] = ((clip_size * 430 + 4095) // 4096) * 4096
        registry.data['generation'] = registry.data.get('generation', 0) + 1
        generation = registry.data['generation']
        registry.save()
    wait_generation(generation, volume=key)
    free = shutil.disk_usage(ROOT / 'media/recordings').free
    hour = clip_size * 360
    assert 0 < free < hour, (free, hour)
    restarted = time.time()
    compose_run(['up', '-d', '--pull', 'never'])
    cid = container()
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        remaining = sql("SELECT count(*) FROM recordings WHERE id LIKE 'synthetic-seed-%'")[0][0]
        if remaining < 370:
            logs = command(['docker', 'logs', cid], timeout=15)
            assert 'Less than 1 hour of recording space left' in logs, logs[-6000:]
            assert 'Cleaned up' in logs, logs[-6000:]
            break
        time.sleep(5)
    else:
        raise AssertionError('unmodified Frigate storage maintenance did not remove seeded recordings')
    assert time.time() - restarted >= 295, 'cleanup did not use the normal five-minute loop'
    removed = sum(not path.exists() for path in seed_paths)
    assert removed == 370 - remaining and removed > 300, (removed, remaining)
    after_cleanup = time.time()
    wait_recording(db, after_cleanup)
    assert sql('PRAGMA integrity_check')[0][0] == 'ok'
    assert sql('PRAGMA wal_checkpoint(TRUNCATE)')[0][0] == 0
    Path(ROOT / 'config/state-write-after-cleanup').write_text('config remains writable')
    row = next(row for row in Registry().data['projects'].values() if row['path'] == str(ROOT / 'media'))
    verify_tree(str(ROOT / 'media'), row['project'])
    assert read_quotas(row['mount'])[row['project']]['used'] > 0
    assert not Path('/run/reefy/storage-pressure/hold.json').exists()
    compose_run(['down'])
    command(['docker', 'rm', '--force', NAME + '-sql'])
    (ROOT / 'config/synthetic-sql.sock').unlink()
    print(json.dumps({'image': IMAGE, 'resolved_image': command(['docker', 'image', 'inspect', IMAGE, '--format', '{{.Id}}']).strip(),
                      'normal_maintainer_seconds': time.time() - restarted,
                      'deleted_segments': removed, 'segment_bytes': clip_size,
                      'recordings_continue_and_database_integrity': 'passed'}))


if __name__ == '__main__':
    import argparse
    import re
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', default=NAME)
    parser.add_argument('--bitrate', type=int, choices=(750, 1000, 1250), default=BITRATE)
    parser.add_argument('--skip-pull', action='store_true')
    options = parser.parse_args()
    assert re.fullmatch(r'synthetic-frigate(?:-[a-z]+)?', options.name)
    NAME = options.name
    ROOT = Path('/mnt/reefy-data/apps') / NAME
    COMPOSE = '/tmp/' + NAME + '.json'
    BITRATE, SKIP_PULL = options.bitrate, options.skip_pull
    try:
        run()
    except Exception:
        from dataclasses import asdict
        # Preserve the actual failing filesystem, quota and pool evidence before
        # any recovery or reboot can hide the cause of an image unpack failure.
        for path in ('/mnt/reefy-data', '/mnt/reefy-data/docker/overlay2', '/tmp'):
            status = os.statvfs(path)
            print(json.dumps({'path': path, 'available_bytes': status.f_bavail * status.f_frsize,
                              'total_bytes': status.f_blocks * status.f_frsize,
                              'available_inodes': status.f_favail}), file=sys.stderr)
        print(json.dumps({'pool': asdict(physical_sample()),
                          'quotas': read_quotas('/mnt/reefy-data')}), file=sys.stderr)
        status_path = Path('/run/reefy/storage-pressure/status.json')
        if status_path.exists():
            print(status_path.read_text(), file=sys.stderr)
        for evidence in ('hold.json', 'session.json'):
            path = Path('/run/reefy/storage-pressure') / evidence
            if path.exists():
                print(evidence + ': ' + path.read_text(), file=sys.stderr)
        print(json.dumps({'persisted_peak': Registry().data.get('peak_bytes_per_second')}), file=sys.stderr)
        try:
            cid = container()
            if cid:
                print(command(['docker', 'logs', '--tail', '80', cid]), file=sys.stderr)
        except Exception as diagnostic_error:
            print('Docker diagnostics unavailable: ' + str(diagnostic_error), file=sys.stderr)
        raise
