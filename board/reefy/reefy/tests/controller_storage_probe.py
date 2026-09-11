#!/usr/bin/env python3
"""Actual systemd/Docker integration on a disposable firmware QEMU device."""
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, '/usr/lib/reefy')
from reefy import shared
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
    print(json.dumps(results, sort_keys=True))


if __name__ == '__main__':
    try:
        run()
    except Exception:
        print(command(['journalctl', '-u', 'reefy-storage-activate', '-u', 'docker',
                       '-u', 'reefy-storage-guard', '-u', 'reefy-storage-watchdog',
                       '--no-pager', '-n', '160']), file=sys.stderr)
        raise
