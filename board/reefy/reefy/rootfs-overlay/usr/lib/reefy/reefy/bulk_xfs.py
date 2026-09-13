"""Bounded XFS commands and inode attributes for bulk-only protection.

No Docker configuration, process stopping, remounting or data deletion.
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

from reefy.bulk_policy import StorageError as PressureError, QUANTUM, parse_thin_sample


PROJINHERIT = 0x200
STATE_DIR = '/mnt/reefy-data/state/bulk-storage'
RUN_DIR = '/run/reefy/bulk-storage'


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
            or not isinstance(hard, int) or hard <= 0 or hard % QUANTUM):
        raise ValueError('invalid nonzero XFS project limit')
    command(['xfs_quota', '-x', '-c',
             f'limit -p bsoft=0 bhard={hard // 1024}k {project}', mountpoint])


def physical_sample(pool='reefy-reefy_pool-tpool', *, timeout=10):
    deadline = time.monotonic() + timeout
    status = command(['dmsetup', 'status', '--noflush', pool], timeout=timeout)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('physical storage sample deadline exceeded')
    table = command(['dmsetup', 'table', pool], timeout=remaining)
    return parse_thin_sample(status, table)


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
        flags = values[0]
        if is_directory:
            flags = flags | PROJINHERIT if project else flags & ~PROJINHERIT
        if values[3] == project and values[0] == flags:
            return False
        values[0], values[3] = flags, project
        buffer = ctypes.create_string_buffer(struct.pack('=QIIII', *values), 24)
        self._call(469, path, buffer)
        return True

