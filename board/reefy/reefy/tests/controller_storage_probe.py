#!/usr/bin/env python3
"""Actual systemd/Docker integration on a disposable firmware QEMU device."""
import glob
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, '/usr/lib/reefy')
from reefy import shared
from reefy.dataplane import DataPlane
from reefy.storage_admission import reservation
from reefy.storage_quota import (Registry, RUN_DIR, FileAttributes, atomic_json,
                                 command, physical_sample, read_quotas, verify_tree)
from reefy.storage_runtime import LAYER_INITIAL_SIZE
from reefy.storage_service import ensure_volume


def wait_ready(timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            registry = Registry()
            with open(RUN_DIR + '/status.json') as source:
                status = json.load(source)
            if (registry.data.get('active') and not registry.data.get('activation_pending')
                    and not os.path.exists(RUN_DIR + '/hold.json')
                    and time.monotonic() - status['sampled_monotonic'] < 20):
                command(['docker', 'info'])
                return registry, status
        except Exception:
            pass
        time.sleep(1)
    raise AssertionError('storage controller did not become ready')


def log_rotation(image):
    for managed in (False, True):
        name = 'synthetic-managed-logger' if managed else 'synthetic-default-logger'
        writer = ('echo synthetic-start; dd if=/dev/zero bs=8192 count=11000 2>/dev/null '
                  '| tr "\\000" x; echo; echo synthetic-end; sleep 3600')
        if managed:
            compose = {'services': {'logger': {'image': image, 'command': ['sh', '-c', writer],
                'logging': {'driver': 'json-file', 'options': {'max-size': '20m', 'max-file': '3'}}}}}
            path = '/tmp/synthetic-logger.json'
            atomic_json(path, compose)
            ok, output = DataPlane._run_compose_command(path, name, ['up', '-d', '--pull', 'never'], 120)
            assert ok, output
            name = command(['docker', 'ps', '-q', '--filter',
                            'label=com.docker.compose.project=' + name]).strip()
        else:
            with reservation('probe-log-create', 2 * LAYER_INITIAL_SIZE):
                command(['docker', 'run', '-d', '--name', name, image, 'sh', '-c', writer])
        try:
            deadline = time.monotonic() + 120
            while 'synthetic-end' not in command(['docker', 'logs', '--tail', '1', name], timeout=15):
                assert time.monotonic() < deadline, 'log writer did not finish'
                time.sleep(1)
            row = json.loads(command(['docker', 'inspect', name]))[0]
            assert row['HostConfig']['LogConfig']['Config'] == {'max-size': '20m', 'max-file': '3'}
            logs = glob.glob(row['LogPath'] + '*')
            assert len(logs) == 3, logs
            assert all(os.stat(path).st_size <= 20 * 1024**2 + 32768 for path in logs)
            assert not any(b'synthetic-start' in Path(path).read_bytes() for path in logs)
            assert row['State']['Running']
        finally:
            command(['docker', 'rm', '--force', name])


def cache_migration(image, policy):
    name, path = 'synthetic-cache', '/tmp/synthetic-cache-compose.json'
    compose = {'services': {'recorder': {'image': image, 'restart': 'always',
        'command': ['sh', '-c', 'mkdir -p /tmp/cache; test -f /tmp/cache/pending || echo preserved > /tmp/cache/pending; exec sleep 3600']}}}
    atomic_json(path, compose)
    ok, output = DataPlane._run_compose_command(path, name, ['up', '-d', '--pull', 'never'], 120)
    assert ok, output
    old = command(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=' + name]).strip()
    deadline = time.monotonic() + 20
    while True:
        try:
            assert command(['docker', 'exec', old, 'cat', '/tmp/cache/pending']).strip() == 'preserved'
            break
        except Exception:
            assert time.monotonic() < deadline
            time.sleep(0.2)
    destination = '/mnt/reefy-data/apps/synthetic-cache/cache'
    policy['app_volumes'].append({'path': destination})
    policy['volume_storage_classes'][destination] = 'bulk'
    atomic_json(shared.desired_state_path(), policy)
    Path(destination).mkdir(parents=True)
    ensure_volume(destination, 'bulk')
    compose['services']['recorder']['volumes'] = [destination + ':/tmp/cache']
    atomic_json(path, compose)
    ok, output = DataPlane._run_compose_command(path, name, ['up', '-d', '--pull', 'never'], 120)
    assert ok, output
    current = command(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=' + name]).strip()
    assert current != old
    assert Path(destination, 'pending').read_text().strip() == 'preserved'
    row = json.loads(command(['docker', 'inspect', current]))[0]
    assert row['State']['Running'] and row['HostConfig']['RestartPolicy']['Name'] == 'always'
    command(['docker', 'rm', '--force', current])


def run():
    results = {}
    # Fetch while still legacy. Network failure is a setup failure, never a pass.
    command(['docker', 'pull', 'busybox:1.37.0'], timeout=180)
    image = json.loads(command(['docker', 'image', 'inspect', 'busybox:1.37.0']))[0]['Id']
    command(['systemctl', 'stop', 'reefy-reconciler.service'])
    media = '/mnt/reefy-data/apps/synthetic-recorder/media'
    config = '/mnt/reefy-data/apps/synthetic-recorder/config'
    for path in (media, config):
        Path(path).mkdir(parents=True, exist_ok=True)
        Path(path, 'preserved').write_text('synthetic contents')
    policy = {'storage_pressure_policy': {'version': 1},
              'app_volumes': [{'path': media}, {'path': config}],
              'volume_storage_classes': {media: 'bulk', config: 'state'}}
    atomic_json(shared.desired_state_path(), policy)
    command(['systemctl', 'start', 'reefy-storage-activate.service'], timeout=300)
    registry, status = wait_ready()
    assert not status['allocation']['quiesce']
    assert physical_sample().healthy
    for path in (media, config):
        record = next(r for r in registry.data['projects'].values() if r['path'] == path)
        verify_tree(path, record['project'])
        assert Path(path, 'preserved').read_text() == 'synthetic contents'
    results['live_policy_activation_preserves_existing_files'] = 'passed'
    info = json.loads(command(['docker', 'info', '--format', '{{json .}}']))
    assert info['Driver'] == 'overlay2'
    with open(RUN_DIR + '/docker.json') as source:
        assert f'overlay2.size={LAYER_INITIAL_SIZE}' in json.load(source)['storage-opts']

    with reservation('probe-container-create', 4 * LAYER_INITIAL_SIZE):
        command(['docker', 'run', '-d', '--name', 'storage-probe',
                 '-v', media + ':/media', '-v', config + ':/config',
                 image, 'sleep', '3600'])
    row = json.loads(command(['docker', 'inspect', 'storage-probe']))[0]
    project = FileAttributes().read(os.path.dirname(row['GraphDriver']['Data']['UpperDir']))[3]
    assert project >= registry.data['docker']['base_project'] + 2
    quota = read_quotas('/mnt/reefy-data')[project]
    assert 0 < quota['hard'] <= 2 * 1024**3
    # Compare the actual container statfs view with the governed bind mount.
    output = command(['docker', 'exec', 'storage-probe', 'df', '-Pk', '/media', '/config'])
    assert '/media' in output and '/config' in output, output
    command(['docker', 'exec', 'storage-probe', 'sh', '-c',
             'echo committed > /config/probe; echo segment > /media/probe'])
    assert Path(config, 'probe').read_text().strip() == 'committed'
    results['docker_native_projects_and_governed_bind_mounts'] = 'passed'

    additional = '/mnt/reefy-data/apps/synthetic-new/state'
    Path(additional).mkdir(parents=True)
    ensure_volume(additional, 'state')
    record = next(r for r in Registry().data['projects'].values() if r['path'] == additional)
    assert read_quotas(record['mount'])[record['project']]['hard'] > 1024
    Path(additional, 'first-write').write_text('new app')
    verify_tree(additional, record['project'])
    results['dynamic_volume_registered_before_first_write'] = 'passed'
    command(['docker', 'rm', '--force', 'storage-probe'])
    log_rotation(image)
    results['managed_and_default_log_rotation'] = 'passed'
    cache_migration(image, policy)
    results['pending_cache_survives_new_bind_mount'] = 'passed'
    print(json.dumps(results, sort_keys=True))


if __name__ == '__main__':
    try:
        run()
    except Exception:
        print(command(['journalctl', '-u', 'reefy-storage-activate', '-u', 'docker',
                       '-u', 'reefy-storage-guard', '-u', 'reefy-storage-watchdog',
                       '--no-pager', '-n', '160']), file=sys.stderr)
        raise
