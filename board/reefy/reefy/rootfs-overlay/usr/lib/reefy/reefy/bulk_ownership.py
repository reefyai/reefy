"""Durable root checkpoints; XFS remains authoritative for quota enforcement."""
import contextlib
import ctypes
import errno
import hashlib
import json
import os

from reefy.bulk_policy import StorageError
from reefy.bulk_xfs import RUN_DIR, state_lock

VERIFIED = 'trusted.reefy.bulk_verified'
RESTORING = 'trusted.reefy.bulk_restoring'
VERSION = 1


@contextlib.contextmanager
def root_fd(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def get_marker(fd, name=VERIFIED):
    try:
        return os.getxattr(fd, name)
    except OSError as error:
        if error.errno == errno.ENODATA:
            return None
        raise


def invalidate(fd, name=VERIFIED):
    try:
        os.removexattr(fd, name)
    except OSError as error:
        if error.errno != errno.ENODATA:
            raise
    os.fsync(fd)


def expected_marker(fd, path, filesystem, project):
    return json.dumps({'version': VERSION, 'filesystem': filesystem,
                       'root': path, 'inode': os.fstat(fd).st_ino,
                       'project': project}, sort_keys=True).encode()


def verified(path, filesystem, project):
    with root_fd(path) as fd:
        return get_marker(fd) == expected_marker(fd, path, filesystem, project)


def sync_filesystem(fd):
    """Commit tagging before a checkpoint can become durable on this filesystem."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syncfs(ctypes.c_int(fd)) != 0:
        raise OSError(ctypes.get_errno(), 'bulk filesystem flush failed')


def complete(fd, path, filesystem, project):
    sync_filesystem(fd)
    os.setxattr(fd, VERIFIED, expected_marker(fd, path, filesystem, project))
    os.fsync(fd)


def app_path(root):
    return os.path.dirname(root)


@contextlib.contextmanager
def ownership_locks(roots, timeout=0):
    # The restore and worker use the same locks. Sorted acquisition also covers
    # several app roots sharing one XFS project without deadlocking.
    with contextlib.ExitStack() as stack:
        for app in sorted({app_path(root) for root in roots}):
            key = hashlib.sha256(os.fsencode(app)).hexdigest()
            stack.enter_context(state_lock(f'{RUN_DIR}/ownership-{key}.lock', timeout))
        yield


def require_not_restoring(root):
    with root_fd(app_path(root)) as fd:
        if get_marker(fd, RESTORING) is not None:
            raise StorageError('bulk ownership waits for restore completion')


def invalidate_children(app):
    # Volume roots only, never recording descendants or symlink targets.
    with os.scandir(app) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                with root_fd(entry.path) as fd:
                    invalidate(fd)


@contextlib.contextmanager
def restore_scope(app):
    """A parent xattr survives interrupted extraction of its volume children.

    No JSON completion registry. A failed restore retains the pending marker;
    the normal restore retry clears it only after extracted markers are removed.
    """
    with ownership_locks([os.path.join(app, 'volume')], timeout=5), root_fd(app) as fd:
        os.setxattr(fd, RESTORING, b'1')
        os.fsync(fd)
        invalidate_children(app)
        yield
        # Archives can reintroduce a source checkpoint. Never accept it as
        # certification of a newly restored volume, even for an in-place restore.
        invalidate_children(app)
        invalidate(fd, RESTORING)
