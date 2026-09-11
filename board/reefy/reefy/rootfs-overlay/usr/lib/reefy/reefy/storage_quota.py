"""XFS project ownership, durable migration records and bounded quota commands.

Recursive work is limited to migration/restore. The guard reads quota counters,
not directory trees. Callers must hold affected writers before changing ownership.
"""
import contextlib
import ctypes
import fcntl
import json
import os
import platform
import re
import stat
import struct
import subprocess
import tempfile
import time

from reefy.storage_pressure import PressureError, parse_thin_sample


PROJINHERIT = 0x200
STATE_DIR = '/mnt/reefy-data/state/storage-pressure'
RUN_DIR = '/run/reefy/storage-pressure'


def command(args, timeout=10):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise PressureError(f'{args[0]} failed: {result.stderr.strip()[:300]}')
    return result.stdout


def atomic_json(path, value):
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.storage-', dir=parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def state_lock(path=RUN_DIR + '/mutation.lock', timeout=5):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, 'a') as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PressureError('storage mutation lock timed out')
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def mount_info(path):
    data = json.loads(command([
        'findmnt', '--json', '--target', path, '--output',
        'TARGET,SOURCE,FSTYPE,OPTIONS,UUID,MAJ:MIN']))
    entries = data.get('filesystems') or []
    if len(entries) != 1:
        raise PressureError('ambiguous storage mount')
    mount = entries[0]
    if mount.get('fstype') != 'xfs' or not mount.get('uuid'):
        raise PressureError('project quotas require an identified XFS filesystem')
    return mount


def require_enforcement(mountpoint):
    result = command(['xfs_quota', '-x', '-c', 'state -p', mountpoint])
    if (not re.search(r'Accounting:\s+ON\b', result)
            or not re.search(r'Enforcement:\s+ON\b', result)):
        raise PressureError('XFS project accounting/enforcement is not enabled')


def read_quotas(mountpoint):
    """xfs_quota's unscaled block report is in KiB, including reservations."""
    report = command(['xfs_quota', '-x', '-c', 'report -p -b -n -N', mountpoint])
    quotas = {}
    for line in report.splitlines():
        values = line.split()
        if not values or not re.fullmatch(r'#?\d+', values[0]):
            continue
        if len(values) < 4 or not all(v.isdigit() for v in values[1:4]):
            raise PressureError('malformed project quota report')
        quotas[int(values[0].lstrip('#'))] = {
            'used': int(values[1]) * 1024,
            'soft': int(values[2]) * 1024,
            'hard': int(values[3]) * 1024,
        }
    return quotas


def set_quota(mountpoint, project, hard):
    if (not isinstance(project, int) or not 0 < project < 2**32
            or not isinstance(hard, int) or hard <= 0 or hard % 1024):
        raise ValueError('invalid nonzero XFS project limit')
    command(['xfs_quota', '-x', '-c',
             f'limit -p bsoft=0 bhard={hard}b {project}', mountpoint])


def physical_sample(pool='reefy-reefy_pool-tpool'):
    return parse_thin_sample(
        command(['dmsetup', 'status', '--noflush', pool]),
        command(['dmsetup', 'table', pool]))


class FileAttributes:
    """Linux 6.18 file attributes, including symlinks without following them.

    Reefy's x86_64 kernel has file_getattr/file_setattr (468/469). The ABI's
    24-byte struct is QIIII. Path-based calls also avoid opening FIFO/device
    inodes during a walk. Refuse an unknown ABI rather than guessing a syscall.
    """
    def __init__(self):
        if platform.system() != 'Linux' or platform.machine() != 'x86_64':
            raise PressureError('unsupported file-attribute syscall ABI')
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.syscall.restype = ctypes.c_long

    def _call(self, number, path, buffer):
        result = self.libc.syscall(
            ctypes.c_long(number), ctypes.c_int(-100),
            ctypes.c_char_p(os.fsencode(path)), ctypes.byref(buffer),
            ctypes.c_size_t(24), ctypes.c_uint(0x100))  # AT_SYMLINK_NOFOLLOW
        if result < 0:
            raise OSError(ctypes.get_errno(), 'XFS inode attributes failed', path)

    def read(self, path):
        buffer = ctypes.create_string_buffer(24)
        self._call(468, path, buffer)
        return list(struct.unpack('=QIIII', buffer.raw))

    def assign(self, path, project, is_directory):
        values = self.read(path)
        flags = values[0] | PROJINHERIT if is_directory else values[0]
        if values[3] == project and values[0] == flags:
            return False
        values[0], values[3] = flags, project
        buffer = ctypes.create_string_buffer(struct.pack('=QIIII', *values), 24)
        self._call(469, path, buffer)
        return True


def mount_targets(path='/proc/self/mountinfo'):
    """Include same-filesystem bind mounts that os.path.ismount cannot detect."""
    try:
        with open(path) as source:
            rows = source.readlines()
    except FileNotFoundError:
        if platform.system() == 'Linux':
            raise PressureError('mount inventory unavailable')
        return set()  # portable filesystem unit tests, never a Linux fallback
    targets = set()
    for row in rows:
        fields = row.split()
        if len(fields) < 7 or '-' not in fields:
            raise PressureError('malformed mount inventory')
        targets.add(re.sub(r'\\([0-7]{3})',
                           lambda match: chr(int(match[1], 8)), fields[4]))
    return targets


def owned_tree(root, excluded=()):
    """Do not follow symlinks or cross any nested mount, including bind mounts."""
    root = os.path.abspath(root)
    excluded = {os.path.abspath(p) for p in excluded if os.path.abspath(p) != root}
    excluded.update(mount_targets() - {root})
    device = os.lstat(root).st_dev
    pending = [root]
    while pending:
        path = pending.pop()
        if path in excluded:
            continue
        metadata = os.lstat(path)
        if path != root and (metadata.st_dev != device or os.path.ismount(path)):
            continue
        yield path, metadata
        if stat.S_ISDIR(metadata.st_mode):
            with os.scandir(path) as entries:
                pending.extend(entry.path for entry in entries)


def check_hardlinks(root, excluded=()):
    links = {}
    for path, info in owned_tree(root, excluded):
        if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1:
            key = (info.st_dev, info.st_ino)
            found, expected = links.get(key, (0, info.st_nlink))
            links[key] = (found + 1, expected)
    if any(found != expected for found, expected in links.values()):
        raise PressureError('hardlink crosses a governed volume boundary')


def assign_tree(root, project, excluded=(), progress=None, attributes=None):
    attributes = attributes or FileAttributes()
    check_hardlinks(root, excluded)
    count = changed = 0
    for path, info in owned_tree(root, excluded):
        changed += attributes.assign(path, project, stat.S_ISDIR(info.st_mode))
        count += 1
        if progress and count % 25000 == 0:
            progress(count, changed)
    if progress:
        progress(count, changed)
    return count, changed


def verify_tree(root, project, excluded=(), attributes=None):
    attributes = attributes or FileAttributes()
    count = 0
    for path, info in owned_tree(root, excluded):
        flags, _, _, found, _ = attributes.read(path)
        if found != project or (stat.S_ISDIR(info.st_mode) and not flags & PROJINHERIT):
            raise PressureError('incomplete project ownership/inheritance')
        count += 1
    return count


def flush_filesystem(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.syncfs(fd):
            raise OSError(ctypes.get_errno(), 'metadata flush failed')
    finally:
        os.close(fd)


class Registry:
    """Persist ownership before tagging; retain retired IDs to prevent reuse.

    A completed record skips recursive work on normal boots. Mount identity,
    root attributes and kernel enforcement still need verification each boot.
    The caller serializes writes; slow tree walks never hold the guard lock.
    """
    def __init__(self, path=STATE_DIR + '/registry.json'):
        self.path = path
        try:
            with open(path) as source:
                self.data = json.load(source)
        except FileNotFoundError:
            self.data = {'version': 1, 'active': False, 'projects': {}}
        if self.data.get('version') != 1 or not isinstance(self.data.get('projects'), dict):
            raise PressureError('unsupported or corrupt project registry')

    def save(self):
        atomic_json(self.path, self.data)

    def register(self, path, mount, storage_class, occupied=()):
        path = os.path.abspath(path)
        identity = mount['uuid'] + ':' + path
        records = self.data['projects']
        if identity not in records:
            taken = set(occupied) | {v['project'] for v in records.values()}
            # Docker overlay2 owns its range starting at 2**20. Never allocate
            # from it, and still inspect existing XFS IDs for older assignments.
            project = next((i for i in range(1024, 2**20) if i not in taken), None)
            if project is None:
                raise PressureError('project ID range exhausted')
            records[identity] = {'path': path, 'filesystem': mount['uuid'],
                                 'project': project, 'complete': False}
        record = records[identity]
        record.update(storage_class=storage_class, mount=mount['target'],
                      device=mount['maj:min'], retired=False)
        self.save()
        return identity, record

    def complete(self, identity):
        self.data['projects'][identity]['complete'] = True
        self.save()
