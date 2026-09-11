"""Preserve data when a managed bind mount replaces container-local storage.

The old container is kept stopped with restart disabled until Compose replaces
it. A failed or interrupted copy is retried from that unchanged source; it never
signals success by deleting recording segments or the old writable layer.
"""
import json
import os

from reefy.storage_admission import reservation
from reefy.storage_pressure import PressureError
from reefy.storage_quota import Registry, command, verify_tree


def bind_mounts(service):
    for volume in service.get('volumes') or []:
        if isinstance(volume, str):
            pieces = volume.split(':')
            if len(pieces) >= 2 and pieces[0].startswith('/'):
                yield pieces[0], pieces[1], len(pieces) == 3 and 'ro' in pieces[2].split(',')
        elif isinstance(volume, dict) and volume.get('type') == 'bind':
            yield volume['source'], volume['target'], bool(volume.get('read_only'))


def preserve_new_mounts(compose, project_name):
    registry = Registry()
    if not registry.data.get('active'):
        return
    ids = command(['docker', 'ps', '--all', '--quiet', '--filter',
                   'label=com.docker.compose.project=' + project_name]).split()
    if not ids:
        return
    containers = json.loads(command(['docker', 'inspect', '--size', *ids], timeout=60))
    for container in containers:
        name = container['Config'].get('Labels', {}).get('com.docker.compose.service')
        service = (compose.get('services') or {}).get(name)
        if service is None:
            continue
        current = {mount['Destination'] for mount in container.get('Mounts', [])}
        for source, target, readonly in bind_mounts(service):
            if readonly or not source.startswith('/mnt/reefy-data/apps/'):
                continue
            # This migration is for old writable-layer paths. Existing binds,
            # named volumes and nested mounts retain their own lifecycle.
            if any(target == existing or target.startswith(existing.rstrip('/') + '/')
                   for existing in current):
                continue
            records = [(key, value) for key, value in registry.data['projects'].items()
                       if value['path'] == source and not value.get('retired')]
            if len(records) != 1 or not records[0][1].get('complete'):
                raise PressureError('new bind destination is not governed')
            identity, record = records[0]
            if os.path.realpath(source) != source:
                raise PressureError('new bind destination is not a real directory')
            # No source mutation or container removal until the complete copy
            # succeeds. Disabling restart persists the barrier through reboot.
            command(['docker', 'update', '--restart=no', container['Id']])
            command(['docker', 'stop', '--time', '30', container['Id']], timeout=40)
            size = container.get('SizeRw')
            if type(size) is not int or size < 0:
                raise PressureError('cannot bound container-local data migration')
            budget = ((size + 64 * 1024**2 + 1023) // 1024) * 1024
            with reservation('new-bind-copy', budget,
                             storage_class=record['storage_class'], target=identity):
                try:
                    command(['docker', 'cp', '--archive',
                             container['Id'] + ':' + target.rstrip('/') + '/.', source],
                            timeout=1800)
                except PressureError as error:
                    # Docker explicitly reports an absent source path. All
                    # other errors (including partial copies) keep startup held.
                    if 'Could not find the file' not in str(error):
                        raise
                verify_tree(source, record['project'])
