#!/usr/bin/env python3
"""Internal operator preparation/checks only; never mount, copy or alter devices."""
import argparse
import json
import os
from pathlib import Path
import re

GIB = 1024**3
MIN_BUILD_BYTES = 1280 * GIB
BULK = {'cache': '/buildroot-cache', 'work': '/_work', 'tmp': '/tmp',
        'var-tmp': '/var/tmp', 'diag': '/actions-runner/_diag', 'docker': '/var/lib/docker',
        'containerd': '/var/lib/containerd'}


class StorageError(ValueError):
    pass


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or any(ord(c) < 32 for c in str(path)):
        raise StorageError('storage paths must be absolute, without control characters')
    return path.resolve()


def layout(build_root, vm_roots):
    root = absolute(build_root)
    vms = [absolute(p) for p in vm_roots]
    if root == Path('/') or not vms:
        raise StorageError('require a dedicated build root and VM roots')
    for vm in vms:
        if vm == root or vm.is_relative_to(root) or root.is_relative_to(vm):
            raise StorageError('build and VM paths must not contain each other')
    builders = {}
    for role in ('primary', 'secondary'):
        mounts = [dict(type='bind', source=str(root/role/name), target=target,
                       read_only=False, bind={'create_host_path': False})
                  for name, target in BULK.items()]
        mounts.append(dict(type='bind', source=str(root/'shared-version'),
                           target='/buildroot-cache/shared-version', read_only=False,
                           bind={'create_host_path': False}))
        if role == 'secondary':
            mounts.append(dict(type='bind', source=str(root/'primary/cache'),
                               target='/seed-cache', read_only=True,
                               bind={'create_host_path': False}))
        builders[role] = {'services': {'app': {'volumes': mounts}}}
    return dict(build_root=str(root), runner_engine_root=str(root/'runner-engine'),
                vm_roots=list(map(str, vms)),
                minimum_build_bytes=MIN_BUILD_BYTES, reserve_percent=20,
                compose_overrides=builders)


def unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def mount_table(text):
    mounts = []
    for line in text.splitlines():
        fields = line.split()
        try:
            separator = fields.index('-')
            device = fields[2]
            if not re.fullmatch(r'\d+:\d+', device):
                raise ValueError()
            mounts.append(dict(device=device, target=Path(unescape(fields[4])),
                               options=fields[5].split(','), fstype=fields[separator+1]))
        except (ValueError, IndexError):
            raise StorageError('incomplete mount inventory') from None
    if not mounts:
        raise StorageError('empty mount inventory')
    return mounts


def mount_for(path, mounts):
    candidates = [m for m in mounts if path == m['target'] or path.is_relative_to(m['target'])]
    if not candidates:
        raise StorageError('path has no known mount')
    deepest = max(len(m['target'].parts) for m in candidates)
    matches = [m for m in candidates if len(m['target'].parts) == deepest]
    if len(matches) != 1:
        raise StorageError('ambiguous mount inventory')
    return matches[0]


def physical_leaves(device, sysfs=Path('/sys'), seen=None):
    """Resolve every DM/LUKS/LVM layer and partitions to whole physical disks."""
    seen = set() if seen is None else set(seen)
    if device in seen:
        raise StorageError('cyclic block ancestry')
    seen.add(device)
    try:
        node = (sysfs/'dev/block'/device).resolve(strict=True)
        if (node/'dev').read_text().strip() != device:
            raise StorageError('block identity changed during inventory')
        if (node/'partition').exists():
            return physical_leaves((node.parent/'dev').read_text().strip(), sysfs, seen)
        slaves = node/'slaves'
        if not slaves.is_dir():
            raise StorageError('missing block ancestry directory')
        children = sorted(slaves.iterdir())
        if children:
            result = set()
            for child in children:
                result |= physical_leaves((child/'dev').read_text().strip(), sysfs, seen)
            return result
        # Loop, virtual disks and unknown empty DM/MD graphs cannot establish
        # independent physical backing. Never infer separation from their names.
        if re.fullmatch(r'nvme\d+n\d+', node.name):
            # Namespaces on one NVMe controller share media and queues.
            controller = (node/'device').resolve(strict=True)
            if not re.fullmatch(r'nvme\d+', controller.name):
                raise StorageError('NVMe controller ancestry is unknown')
            return {'nvme:' + controller.name}
        if re.fullmatch(r'sd[a-z]+', node.name):
            # Collapse multipath aliases when the kernel exposes a WWID.
            wwid = node/'device/wwid'
            return {'scsi:' + wwid.read_text().strip()} if wwid.is_file() else {device}
        raise StorageError('physical backing is unknown or virtual')
    except OSError as error:
        raise StorageError('block ancestry is unavailable') from error


def inspect_path(value, mounts, sysfs=Path('/sys')):
    path = absolute(value)
    if not path.is_dir():
        raise StorageError('required storage directory is missing')
    mount = mount_for(path, mounts)
    if mount['fstype'] not in ('ext4', 'xfs'):
        raise StorageError('require a supported persistent local filesystem')
    if 'rw' not in mount['options']:
        raise StorageError('storage mount is not read-write')
    return dict(path=str(path), mount=str(mount['target']),
                device=mount['device'], fstype=mount['fstype'],
                leaves=sorted(physical_leaves(mount['device'], sysfs)))


def check(spec, mountinfo=None, sysfs=Path('/sys'), statvfs=os.statvfs):
    # Render from roots, rather than trusting arbitrary paths/limits in a JSON
    # file. An edited spec cannot weaken checks or insert engine socket mounts.
    expected = layout(spec['build_root'], spec['vm_roots'])
    if spec != expected:
        raise StorageError('layout differs from the reviewed generated shape')
    mounts = mount_table(Path('/proc/self/mountinfo').read_text() if mountinfo is None else mountinfo)
    build = inspect_path(spec['build_root'], mounts, sysfs)
    if build['mount'] != build['path']:
        raise StorageError('build root is not an independently mounted filesystem')
    vms = [inspect_path(p, mounts, sysfs) for p in spec['vm_roots']]
    for vm in vms:
        if set(build['leaves']) & set(vm['leaves']):
            raise StorageError('build and VM storage share physical backing')
    capacity = statvfs(spec['build_root'])
    size = capacity.f_blocks * capacity.f_frsize
    free = capacity.f_bavail * capacity.f_frsize
    if size < MIN_BUILD_BYTES or free * 100 < size * 20:
        raise StorageError('build capacity or twenty-percent free reserve is insufficient')
    inspect_engine = inspect_path(spec['runner_engine_root'], mounts, sysfs)
    if inspect_engine['mount'] != build['mount']:
        raise StorageError('outer runner engine escapes dedicated storage')
    for override in spec['compose_overrides'].values():
        for bind in override['services']['app']['volumes']:
            source = inspect_path(bind['source'], mounts, sysfs)
            if source['mount'] != build['mount'] or source['leaves'] != build['leaves']:
                raise StorageError('builder bulk-write path escapes dedicated storage')
            if not absolute(bind['source']).is_relative_to(absolute(spec['build_root'])):
                raise StorageError('builder path resolves outside build root')
    return dict(storage_ready=True, build=build, vm=vms,
                capacity_bytes=size, available_bytes=free,
                configuration_applied=False, qualification_passed=False)


def check_container_mounts(spec, observed, mountinfo=None, sysfs=Path('/sys')):
    """Post-cutover read-only Docker Mounts verification; no daemon calls."""
    if set(observed) != {'primary', 'secondary'}:
        raise StorageError('require both builder mount inventories')
    if spec != layout(spec['build_root'], spec['vm_roots']):
        raise StorageError('unrecognized layout')
    mounts = mount_table(Path('/proc/self/mountinfo').read_text() if mountinfo is None else mountinfo)
    build = inspect_path(spec['build_root'], mounts, sysfs)
    for role, override in spec['compose_overrides'].items():
        container = observed[role]
        rows = container['Mounts']
        graph = container['GraphDriver']
        if (graph['Name'] != 'overlay2' or
                not all(graph['Data'].get(k) for k in ('UpperDir', 'WorkDir', 'LowerDir'))):
            raise StorageError('outer runner writable layer backing is unverified')
        layers = [graph['Data']['UpperDir'], graph['Data']['WorkDir'],
                  *graph['Data']['LowerDir'].split(':')]
        for layer in layers:
            backing = inspect_path(layer, mounts, sysfs)
            if (backing['mount'] != build['mount'] or backing['leaves'] != build['leaves']
                    or not absolute(backing['path']).is_relative_to(absolute(spec['runner_engine_root']))):
                raise StorageError('outer runner writable layer escapes dedicated storage')
        targets = [row['Destination'] for row in rows]
        if len(targets) != len(set(targets)):
            raise StorageError('duplicate container mount destination')
        for expected in override['services']['app']['volumes']:
            matches = [r for r in rows if r['Destination'] == expected['target']]
            if len(matches) != 1:
                raise StorageError('missing builder bulk-write mount')
            row = matches[0]
            if (row['Type'] != 'bind' or absolute(row['Source']) != absolute(expected['source'])
                    or row['RW'] != (not expected['read_only'])):
                raise StorageError('builder mount source or mode mismatch')
        if any(r['Destination'] in ('/var/run/docker.sock', '/run/docker.sock') for r in rows):
            raise StorageError('host engine socket must not be introduced')
    return {'container_mounts_match': True, 'configuration_applied': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan')
    plan.add_argument('--build-root', required=True)
    plan.add_argument('--vm-root', action='append', required=True)
    verify = commands.add_parser('check')
    verify.add_argument('layout', type=Path)
    mounted = commands.add_parser('check-containers')
    mounted.add_argument('layout', type=Path)
    mounted.add_argument('inventory', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'plan':
            result = dict(layout=layout(args.build_root, args.vm_root),
                          storage_ready=False, configuration_applied=False)
        elif args.command == 'check':
            payload = json.loads(args.layout.read_text())
            result = check(payload.get('layout', payload))
        else:
            payload = json.loads(args.layout.read_text())
            spec = payload.get('layout', payload)
            result = {**check(spec), **check_container_mounts(
                spec, json.loads(args.inventory.read_text()))}
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(json.dumps({'storage_ready': False, 'configuration_applied': False,
                          'error': str(error)}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
