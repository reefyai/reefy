"""Inventory Docker overlay2 quota domains while the daemon is stopped.

Docker 28's quota.NewControl uses the driver-home project as its allocation
offset and discovers existing layer projects at startup. It resets base+1 as
an empty feature probe, so that ID must never own files. Existing distinct
container IDs are preserved. Shared legacy roots get fresh native-range IDs
before Docker starts; the migration worker tags their existing descendants.

Reference: https://github.com/moby/moby/blob/v28.3.2/quota/projectquota.go
"""
import json
import os
from pathlib import Path
import re

from reefy.storage_pressure import PressureError
from reefy.storage_quota import RUN_DIR, FileAttributes, atomic_json, mount_info, read_quotas


NATIVE_BASE = 2**20
LAYER_SIZE = 2 * 1024**3
# Small creation allowance; managed creation reserves it before Docker runs.
# The guard can grow each layer to LAYER_SIZE using the shared pool ledger.
LAYER_INITIAL_SIZE = 16 * 1024**2
IDENTITY = re.compile(r'^[a-f0-9]{64}$')


def register_runtime(registry, *, docker_root='/mnt/reefy-data/docker', attributes=None):
    """Return runtime policies; the caller then migrates all domains together."""
    attributes = attributes or FileAttributes()
    docker = Path(docker_root)
    overlay = docker / 'overlay2'
    overlay.mkdir(parents=True, exist_ok=True)
    mount = mount_info(str(overlay))
    quotas = read_quotas(mount['target'])
    settings = registry.data.setdefault('docker', {})
    if settings and settings.get('filesystem') != mount['uuid']:
        raise PressureError('Docker filesystem changed without ownership migration')
    base = settings.get('base_project')
    if base is None:
        base = max(NATIVE_BASE, max(quotas, default=0) + 1)
        if base + 2 >= 2**32:
            raise PressureError('Docker project range exhausted')
        settings.update(filesystem=mount['uuid'], base_project=base)
        registry.save()
    if quotas.get(base + 1, {}).get('used', 0):
        raise PressureError('Docker feature-probe project unexpectedly owns blocks')
    registry.register(str(overlay), mount, 'runtime', quotas, preferred_project=base)
    policies = {str(overlay): 'runtime'}
    # Docker may reuse its own high IDs after removed layers disappear across
    # a daemon restart. Reefy app IDs remain permanent. Retire native mappings
    # only after both the old directory and all charged blocks are gone.
    for identity, record in list(registry.data['projects'].items()):
        if (record.get('native_docker') and not os.path.lexists(record['path'])
                and not quotas.get(record['project'], {}).get('used', 0)):
            del registry.data['projects'][identity]
    registry.save()
    taken = set(quotas) | {v['project'] for v in registry.data['projects'].values()}
    taken.update((base, base + 1))
    containers = docker / 'containers'
    for container in sorted(containers.iterdir()) if containers.exists() else []:
        if not container.is_dir():
            continue
        if not IDENTITY.fullmatch(container.name):
            raise PressureError('unrecognized Docker container identity')
        with (container / 'config.v2.json').open() as source:
            config = json.load(source)
        if config.get('Driver') != 'overlay2':
            raise PressureError('storage guard currently requires Docker overlay2')
        pointer = docker / 'image/overlay2/layerdb/mounts' / container.name / 'mount-id'
        layer = pointer.read_text().strip()
        if not IDENTITY.fullmatch(layer):
            raise PressureError('unrecognized Docker writable layer identity')
        root = str(overlay / layer)
        if os.path.realpath(root) != root or not os.path.isdir(root):
            raise PressureError('Docker writable layer root is unavailable')
        identity = mount['uuid'] + ':' + root
        existing = registry.data['projects'].get(identity)
        project = attributes.read(root)[3]
        if existing:
            project = existing['project']
        elif project in (0, base) or project == attributes.read(str(overlay))[3]:
            project = max(taken) + 1
        if project == base + 1:
            raise PressureError('Docker layer owns its reserved feature-probe project')
        taken.add(project)
        _, record = registry.register(root, mount, 'runtime', quotas,
                                      preferred_project=project)
        record['native_docker'] = True
        record['max_hard'] = LAYER_SIZE
        registry.save()
        policies[root] = 'runtime'
    return policies


def configure_daemon(active, *, source='/etc/docker/daemon.json',
                     destination=RUN_DIR + '/docker.json'):
    """Generate boot-local configuration; bootstrap filesystems need no quota.

    Never persist a quota driver option in the immutable generic config: initial
    device bootstrap can run Docker before an XFS thin filesystem exists.
    """
    with open(source) as stream:
        config = json.load(stream)
    options = [value for value in config.get('storage-opts', [])
               if not value.startswith('overlay2.size=')]
    if active:
        config['storage-driver'] = 'overlay2'
        options.append(f'overlay2.size={LAYER_INITIAL_SIZE}')
    if options:
        config['storage-opts'] = options
    else:
        config.pop('storage-opts', None)
    atomic_json(destination, config)


def active_native_projects(registry, *, attributes=None, overlay='/mnt/reefy-data/docker/overlay2'):
    """Read only overlay2's immediate roots, never container directory trees.

    An empty dquot survives removal of its Docker layer. It must not receive
    fresh runway forever merely because its numeric ID is in Docker's range.
    """
    settings = registry.data.get('docker') or {}
    if not settings:
        return set()
    attributes = attributes or FileAttributes()
    projects = set()
    with os.scandir(overlay) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                project = attributes.read(entry.path)[3]
            except FileNotFoundError:
                continue  # Docker removed it after scandir.
            if project >= settings['base_project'] + 2:
                projects.add(project)
    return projects
