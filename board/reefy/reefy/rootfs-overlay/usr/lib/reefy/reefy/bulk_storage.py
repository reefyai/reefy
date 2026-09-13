"""Best-effort bulk quota reconciliation, independent of Docker and MQTT."""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import stat
import time

from reefy import shared
from reefy.bulk_policy import INTERVAL, StorageError, apply_targets, headroom, targets
from reefy.bulk_xfs import (
    FileAttributes, PROJINHERIT, RUN_DIR, STATE_DIR, atomic_json, command,
    mount_info, physical_sample, read_quotas, require_enforcement, set_quota,
    state_lock,
)

APP_ROOT = '/mnt/reefy-data/apps'
REPAIR_INTERVAL = 6 * 3600
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


def repair(roots, project, connection):
    """Worker process: slow walks never hold up physical-space observations."""
    try:
        attributes = FileAttributes()
        excluded = nested_mounts()
        identity = {root: fingerprint(root) for root in roots}
        links = {}
        for root in roots:
            for path, info in walk(root, excluded):
                if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1:
                    key = info.st_dev, info.st_ino
                    seen, expected = links.get(key, (0, info.st_nlink))
                    links[key] = seen + 1, expected
        if any(seen != expected for seen, expected in links.values()):
            raise StorageError('hardlink crosses bulk ownership boundary')
        for verifying in (False, True):
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
                            attributes.assign(path, project, directory)
                        count += 1
                        if count % 4096 == 0:
                            shared.log('bulk-storage', f'Background ownership pass: {count} inodes')
                            time.sleep(0.01)
                    except FileNotFoundError:
                        continue
            if nested_mounts() != excluded:
                raise StorageError('mount inventory changed during bulk repair')
        if any(fingerprint(root) != value for root, value in identity.items()):
            raise StorageError('bulk directory changed during repair')
        connection.send({'ok': True, 'identity': identity})
    except Exception as error:
        connection.send({'ok': False, 'error': str(error)})
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
        self.last_audit = {}
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
            self.last_audit[key] = time.monotonic()
            self.save()
        else:
            self.retry_after[key] = time.monotonic() + 60
            shared.log('bulk-storage', 'Ownership repair incomplete; retaining existing quotas')

    def start_repair(self, key, roots, project):
        if self.worker or time.monotonic() < self.retry_after.get(key, 0):
            return
        context = multiprocessing.get_context('fork')
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=repair, args=(roots, project, send))
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
                    if (record['roots'].get(root) != fingerprint(root)
                            or values[3] != project or not values[0] & PROJINHERIT):
                        ready = False
                audit_due = time.monotonic() - self.last_audit.get(key, -REPAIR_INTERVAL) >= REPAIR_INTERVAL
                if group['roots'] and (not ready or audit_due):
                    self.start_repair(key, group['roots'], project)
                    errors.append('bulk ownership verification pending')
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
        sample_deadline = time.monotonic() + INTERVAL
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
            time.sleep(INTERVAL)


def main():
    with state_lock(timeout=1):
        Guard().run()


if __name__ == '__main__':
    main()
