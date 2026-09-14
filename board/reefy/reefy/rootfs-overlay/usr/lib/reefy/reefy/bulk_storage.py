"""Best-effort bulk quota reconciliation, independent of Docker and MQTT."""
import hashlib
import contextlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import stat
import time

from reefy import shared
from reefy import bulk_ownership as ownership
from reefy.bulk_policy import INTERVAL, SAMPLE_MAX_AGE, StorageError, apply_targets, headroom, targets
from reefy.bulk_xfs import (
    FileAttributes, PROJINHERIT, RUN_DIR, STATE_DIR, atomic_json, command,
    mount_info, physical_sample, read_quotas, require_enforcement, set_quota,
    state_lock,
)

APP_ROOT = '/mnt/reefy-data/apps'
REPAIR_TIMEOUT = 1800
PROJECT_BASE = 0x50000000


def volume_classes(state):
    """An absent class map is not permission to erase installed protection."""
    if state.get('schema_version') == 2:
        entries = state.get('apps', [])
        if not all('volume_storage_classes' in app for app in entries):
            return None
        classes = {}
        for app in entries:
            classes.update(app['volume_storage_classes'])
        return classes
    return state.get('volume_storage_classes')


def valid_root(path):
    if not isinstance(path, str):
        return False
    relative = os.path.relpath(path, APP_ROOT)
    return (isinstance(path, str) and os.path.isabs(path)
            and re.fullmatch(r'[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+', relative)
            and path == os.path.normpath(path)
            and path == os.path.realpath(path) and os.path.isdir(path))


def fingerprint(path):
    value = os.lstat(path)
    return [value.st_dev, value.st_ino]


def nested_mounts():
    with open('/proc/self/mountinfo') as stream:
        return {re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), row.split()[4])
                for row in stream}


def walk(root, excluded):
    """No symlink traversal and no crossing nested mounts, including binds."""
    device = os.lstat(root).st_dev
    yield root, os.lstat(root)
    pending = [os.scandir(root)]
    try:
        while pending:
            try:
                entry = next(pending[-1])
            except StopIteration:
                pending.pop().close()
                continue
            path = entry.path
            if path in excluded:
                raise StorageError('nested mount prevents complete bulk coverage')
            try:
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != device:
                    raise StorageError('bulk filesystem changed during repair')
                yield path, info
                if stat.S_ISDIR(info.st_mode):
                    pending.append(os.scandir(path))
            except FileNotFoundError:
                # Ordinary recording expiry while walking is expected.
                continue
    finally:
        for entries in pending:
            entries.close()


def relevant_mounts(roots):
    """Track backing/ancestor and nested mount identities, not unrelated Docker."""
    result = set()
    with open('/proc/self/mountinfo') as stream:
        for row in stream:
            fields = row.split()
            path = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4])
            if any(path == root or root.startswith(path.rstrip('/') + '/')
                   or path.startswith(root.rstrip('/') + '/') for root in roots):
                # Mount ID, parent, device, filesystem root and target detect
                # replacement/binds even when path and st_dev remain unchanged.
                result.add(tuple(fields[:5]))
    return result


def repair(roots, project, filesystem, connection):
    """Worker process: slow walks never hold up physical-space observations."""
    phase = 'preflight'
    started = time.monotonic()
    examined = changed = 0
    try:
        attributes = FileAttributes()
        with ownership.ownership_locks(roots), contextlib.ExitStack() as stack:
            descriptors = {root: stack.enter_context(ownership.root_fd(root)) for root in roots}
            excluded = nested_mounts()
            mounts = relevant_mounts(roots)
            identity = {root: fingerprint(root) for root in roots}
            for root, fd in descriptors.items():
                ownership.require_not_restoring(root)
                if [os.fstat(fd).st_dev, os.fstat(fd).st_ino] != identity[root]:
                    raise StorageError('bulk directory changed before repair')
                # Prove marker writes work before paying for a recursive walk.
                # An interrupted preflight leaves an invalid checkpoint.
                os.setxattr(fd, ownership.VERIFIED, b'pending')
                ownership.invalidate(fd)
            links = {}
            phase = 'hardlinks'
            for root in roots:
                for path, info in walk(root, excluded):
                    if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1:
                        key = info.st_dev, info.st_ino
                        seen, expected = links.get(key, (0, info.st_nlink))
                        links[key] = seen + 1, expected
            if any(seen != expected for seen, expected in links.values()):
                raise StorageError('hardlink crosses bulk ownership boundary')
            last_progress = time.monotonic()
            for verifying in (False, True):
                phase = 'verify' if verifying else 'assign'
                count = 0
                for root in roots:
                    for path, info in walk(root, excluded):
                        try:
                            directory = stat.S_ISDIR(info.st_mode)
                            if verifying:
                                values = attributes.read(path)
                                if (values[3] != project or (directory and
                                        bool(values[0] & PROJINHERIT) != bool(project))):
                                    raise StorageError('bulk ownership needs another repair pass')
                            else:
                                changed += bool(attributes.assign(path, project, directory))
                            count += 1
                            examined += 1
                            if count % 4096 == 0:
                                if time.monotonic() - last_progress >= 10:
                                    shared.log('bulk-storage', f'Ownership {phase}: {count} inodes')
                                    last_progress = time.monotonic()
                                time.sleep(0.01)
                        except FileNotFoundError:
                            continue
                if relevant_mounts(roots) != mounts:
                    raise StorageError('bulk mount changed during ownership repair')
            if any(fingerprint(root) != value for root, value in identity.items()):
                raise StorageError('bulk directory changed during repair')
            phase = 'checkpoint'
            for root, fd in descriptors.items():
                if project:
                    ownership.complete(fd, root, filesystem, project)
                else:
                    # Persist removal before the parent retires its mapping.
                    ownership.sync_filesystem(fd)
            if relevant_mounts(roots) != mounts or any(
                    fingerprint(root) != value for root, value in identity.items()):
                for fd in descriptors.values():
                    ownership.invalidate(fd)
                raise StorageError('bulk boundary changed while saving checkpoint')
        shared.log('bulk-storage', f'Ownership repair completed: examined={examined} '
                   f'changed={changed} elapsed={time.monotonic() - started:.3f}s')
        connection.send({'ok': True, 'identity': identity})
    except Exception as error:
        connection.send({'ok': False, 'error': str(error), 'phase': phase})
    finally:
        connection.close()


class Guard:
    def __init__(self, state_dir=STATE_DIR, run_dir=RUN_DIR):
        self.registry_path = str(Path(state_dir) / 'projects.json')
        self.status_path = str(Path(run_dir) / 'status.json')
        try:
            self.registry = json.loads(Path(self.registry_path).read_text())
            if self.registry.get('version') != 1:
                raise StorageError('unknown bulk ownership registry')
        except FileNotFoundError:
            self.registry = {'version': 1, 'filesystems': {}}
        self.worker = None
        self.repair_errors = {}
        self.retry_after = {}
        self.last_status = None
        self.attributes = FileAttributes()

    def save(self):
        atomic_json(self.registry_path, self.registry)

    def worker_result(self):
        if not self.worker:
            return
        process, connection, key, roots, project, started = self.worker
        if process.is_alive() and time.monotonic() - started < REPAIR_TIMEOUT:
            return
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                # Never spawn another mutation worker while this one is stuck.
                raise StorageError('background bulk repair is stuck')
        try:
            result = connection.recv() if connection.poll() else {'ok': False}
        except EOFError:
            result = {'ok': False}
        process.join(timeout=1)
        connection.close()
        self.worker = None
        record = self.registry['filesystems'][key]
        if result['ok']:
            for root in roots:
                if project:
                    record['roots'][root] = result['identity'][root]
                else:
                    record['roots'].pop(root, None)
            self.repair_errors.pop(key, None)
            self.save()
        else:
            delay = 300 if result.get('phase') in ('preflight', 'checkpoint') else 60
            self.retry_after[key] = time.monotonic() + delay
            error = result.get('error', 'ownership worker exited without a result')
            self.repair_errors[key] = error
            shared.log('bulk-storage', f'Ownership repair incomplete: {error}; '
                       f'retaining existing quotas, retry in {delay}s')

    def start_repair(self, key, roots, project):
        if self.worker or time.monotonic() < self.retry_after.get(key, 0):
            return
        context = multiprocessing.get_context('fork')
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=repair, args=(roots, project, key, send))
        process.start()
        send.close()
        self.worker = process, receive, key, roots, project, time.monotonic()

    def inventory(self, classes):
        if not isinstance(classes, dict) or any(c not in ('bulk', 'state') for c in classes.values()):
            raise StorageError('invalid volume class policy')
        groups, errors = {}, []
        for path, value in sorted(classes.items()):
            if value != 'bulk':
                continue
            try:
                if not valid_root(path):
                    raise StorageError('bulk path is missing or not a managed directory')
                mount = mount_info(path)
                require_enforcement(mount['target'])
                # Do not tag an empty underlying directory while its app LV
                # exists but is not mounted, including cached-policy boot races.
                lv = 'reefy_backup_' + hashlib.sha1(path.encode()).hexdigest()[:12]
                if os.path.exists('/dev/reefy/' + lv) and mount['target'] != path:
                    raise StorageError('bulk app volume is not mounted')
                table = command(['dmsetup', 'table', mount['source']]).split()
                pool = command(['dmsetup', 'info', '-c', '--noheadings',
                                '--separator', ':', '-o', 'major,minor',
                                'reefy-reefy_pool-tpool']).strip()
                if 'thin' not in table or table[table.index('thin') + 1] != pool:
                    raise StorageError('bulk filesystem is outside the managed thin pool')
                key = mount['uuid']
                group = groups.setdefault(key, {'mount': mount, 'roots': []})
                if group['mount']['maj:min'] != mount['maj:min']:
                    raise StorageError('duplicate mounted XFS UUID')
                group['roots'].append(path)
            except Exception as error:
                errors.append(str(error))
        return groups, errors

    def pass_once(self, classes):
        self.worker_result()
        physical_sample(timeout=4)  # Fail before mutations if the pool is unreadable.
        groups, errors = self.inventory(classes)
        # Retired/pending projects can still own files, including open deleted
        # files. Count them until the kernel releases usage, rather than making
        # their allowance invisible when the desired class changes.
        for key, record in self.registry['filesystems'].items():
            if key not in groups and record.get('mountpoint'):
                try:
                    mount = mount_info(record['mountpoint'])
                    if mount['uuid'] == key:
                        require_enforcement(mount['target'])
                        groups[key] = {'mount': mount, 'roots': []}
                except Exception:
                    errors.append('previous bulk filesystem unavailable')
        usage, current, active = {}, {}, {}
        for key, group in groups.items():
            try:
                mountpoint = group['mount']['target']
                quotas = read_quotas(mountpoint)
                records = self.registry['filesystems']
                if key not in records:
                    project = PROJECT_BASE
                    while project in quotas:
                        project += 1
                    records[key] = {'project': project, 'roots': {}, 'mountpoint': mountpoint}
                    self.save()
                record = records[key]
                project = record['project']
                removed = [root for root in record['roots']
                           if root in classes and classes[root] == 'state' and valid_root(root)]
                if removed:
                    self.start_repair(key, removed, 0)
                    errors.append('bulk class removal pending')
                ready = True
                for root in group['roots']:
                    values = self.attributes.read(root)
                    ownership.require_not_restoring(root)
                    if (not ownership.verified(root, key, project)
                            or values[3] != project or not values[0] & PROJINHERIT):
                        ready = False
                    elif root not in record['roots']:
                        # The marker can commit before worker_result persists
                        # its mapping. Recover that crash window without a scan.
                        record['roots'][root] = fingerprint(root)
                        self.save()
                if group['roots'] and not ready:
                    self.start_repair(key, group['roots'], project)
                    errors.append('bulk ownership verification pending')
                    if key in self.repair_errors:
                        errors.append(self.repair_errors[key])
                quota = quotas.get(project, {'used': 0, 'hard': 0})
                if not group['roots'] and not quota['used']:
                    if quota['hard'] != 4096:
                        set_quota(mountpoint, project, 4096)
                    continue
                if (ready and group['roots']) or quota['hard']:
                    usage[key], current[key] = quota['used'], quota['hard']
                    active[key] = mountpoint, project
            except Exception as error:
                errors.append(str(error))
        # Inventory and ownership checks can be slow. Sample again after them;
        # an old observation must not authorize more growth.
        sample = physical_sample(timeout=4)
        sample_deadline = time.monotonic() + SAMPLE_MAX_AGE
        desired = targets(sample, usage)
        for key in desired:
            if not groups[key]['roots']:
                # No allocation entitlement for retired projects. Their
                # retained usage still counts against the budget until freed.
                desired[key] = max(4096, usage[key] // 4096 * 4096)

        def read(key):
            mountpoint, project = active[key]
            return read_quotas(mountpoint).get(project, {}).get('hard', 0)

        def write(key, limit):
            if current[key] and limit > current[key] and time.monotonic() >= sample_deadline:
                raise StorageError('physical sample expired before quota increase')
            mountpoint, project = active[key]
            set_quota(mountpoint, project, limit)

        apply_targets(current, desired, write, read, allow_growth=not errors)
        return {
            'scope': 'bulk-only', 'stage': 'degraded' if errors else 'active',
            'sample_time': time.time(), 'physical_capacity': sample.capacity,
            'physical_free': sample.capacity - sample.used,
            'withheld_headroom': headroom(sample.capacity),
            'metadata_used': sample.metadata_used, 'metadata_capacity': sample.metadata_capacity,
            'pressure': (sample.capacity - sample.used <= headroom(sample.capacity)
                         or sample.metadata_used * 10 >= sample.metadata_capacity * 9),
            'projects': {key: {'used': usage[key], 'target': desired[key], 'hard': read(key)}
                         for key in desired}, 'errors': sorted(set(errors)),
        }

    def publish(self, status):
        atomic_json(self.status_path, status)
        summary = (status['stage'], status.get('pressure'), tuple(status.get('errors', [])))
        if summary != self.last_status:
            shared.log('bulk-storage', f"Protection {status['stage']}; "
                       f"physical pressure={bool(status.get('pressure'))}; "
                       + '; '.join(status.get('errors', [])))
            self.last_status = summary

    def run(self):
        while True:
            started = time.monotonic()
            try:
                self.worker_result()
                state, _ = shared.load_desired_state()
                classes = volume_classes(state)
                if classes is None:
                    status = {'scope': 'bulk-only', 'stage': 'waiting-policy',
                              'errors': ['No class-aware policy; existing quotas retained']}
                else:
                    status = self.pass_once(classes)
            except Exception as error:
                status = {'scope': 'bulk-only', 'stage': 'degraded', 'errors': [str(error)]}
            status['updated_at'] = time.time()
            self.publish(status)
            # No overlapping passes or accumulated catch-up work.
            time.sleep(max(1, INTERVAL - (time.monotonic() - started)))


def main():
    with state_lock(timeout=1):
        Guard().run()


if __name__ == '__main__':
    main()
