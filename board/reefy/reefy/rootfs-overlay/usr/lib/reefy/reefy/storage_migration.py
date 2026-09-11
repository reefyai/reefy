"""Restartable project migration while affected writers are held at boot.

The caller owns the boot barrier. This worker never stops apps or opens their
startup gate itself, and it never runs as an online pre-tagging background job.
"""
from contextlib import contextmanager
import os
import stat
import time
import tempfile

from reefy.storage_pressure import PressureError, QUANTUM, boundaries
from reefy.storage_quota import (
    FileAttributes, PROJINHERIT, Registry, assign_tree, atomic_json,
    flush_filesystem, mount_info, physical_sample, read_quotas,
    require_enforcement, set_quota, verify_tree, owned_tree, RUN_DIR, command, mount_targets,
)


# Keep metadata-copy runway outside the emergency reserve between checkpoints.
MIGRATION_IN_FLIGHT = 64 * 1024**2


class Migration:
    def __init__(self, *, registry=None, attributes=None, sample=physical_sample,
                 report=None):
        self.registry = registry if registry is not None else Registry()
        self.attributes = attributes if attributes is not None else FileAttributes()
        self.sample = sample
        self.report = report or (lambda value: atomic_json(
            RUN_DIR + '/migration.json', value))
        self.started = time.monotonic()

    def checkpoint(self, phase, **details):
        sample = self.sample()
        # Migration has no app writers. Metadata COW still consumes physical
        # space, so never begin or continue by assuming tagging is free.
        if (not sample.healthy
                or sample.metadata_used * 100 >= sample.metadata_capacity * 80
                or sample.used >= boundaries(sample.capacity).state - MIGRATION_IN_FLIGHT):
            raise PressureError('migration paused: physical or metadata headroom exhausted')
        self.report({'phase': phase, 'elapsed_seconds': time.monotonic() - self.started,
                     **details})

    def prepare(self, policies, *, force_verify=False):
        """Tag owned roots, then close temporary limits before marking complete.

        All roots must already exist on their final mounts. Roots may be nested;
        each walk explicitly excludes the other domains. Completed volumes need
        only mount/root/limit checks on ordinary subsequent boots. Restore calls
        force_verify after extraction before releasing the destination writer.
        """
        roots = sorted(policies)
        inventory = []
        for root in roots:
            if os.path.realpath(root) != root or not os.path.isdir(root):
                raise PressureError('volume root must be a real, mounted directory')
            mount = mount_info(root)
            if '[' in mount.get('source', ''):
                raise PressureError('governed root is an ambiguous bind-mount alias')
            require_enforcement(mount['target'])
            quotas = read_quotas(mount['target'])
            identity, record = self.registry.register(
                root, mount, policies[root], occupied=quotas)
            inventory.append((identity, record))
        # Non-recursive bind views expose hidden mountpoint inodes and old
        # files below mounted app/state volumes. Those blocks still belong to
        # the backing filesystem, even though the ordinary live walk cannot see
        # them. Tag each filesystem's own contents without crossing its mounts.
        with self._filesystem_views(inventory) as views:
            for identity, record in inventory:
                mount = record['mount']
                view = views[mount]
                walk_root = os.path.join(view, os.path.relpath(record['path'], mount))
                excluded = [os.path.join(view, os.path.relpath(other['path'], mount))
                            for _, other in inventory if other['mount'] == mount]
                self._prepare_one(identity, record, excluded, force_verify,
                                  walk_root=os.path.normpath(walk_root))
        return {identity: record for identity, record in inventory}

    @contextmanager
    def _filesystem_views(self, inventory):
        os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
        # Interrupted workers may leave a private bind view. Never delete its
        # contents: unmount only the exact internal mount and remove its empty
        # mountpoint directory. App writers are held throughout migration.
        mounted = mount_targets()
        for name in os.listdir(RUN_DIR):
            if not name.startswith('quota-view-'):
                continue
            path = os.path.join(RUN_DIR, name)
            if os.path.islink(path):
                raise PressureError('unexpected migration view symlink')
            if path in mounted:
                command(['umount', path], timeout=60)
            os.rmdir(path)
        views = {}
        try:
            for _, record in inventory:
                mount = record['mount']
                if mount in views:
                    continue
                view = tempfile.mkdtemp(prefix='quota-view-', dir=RUN_DIR)
                views[mount] = view
                command(['mount', '--bind', mount, view], timeout=60)
                command(['mount', '--make-private', view])
            yield views
        finally:
            for view in reversed(list(views.values())):
                if view in mount_targets():
                    command(['umount', view], timeout=60)
                os.rmdir(view)

    def _prepare_one(self, identity, record, roots, force_verify, *, walk_root=None):
        root, project, mount = record['path'], record['project'], record['mount']
        walk_root = walk_root or root
        info = os.lstat(root)
        flags, _, _, found, _ = self.attributes.read(root)
        quotas = read_quotas(mount)
        quota = quotas.get(project)
        ready = (record.get('complete') and record.get('ownership_version') == 2
                 and record.get('root_inode') == info.st_ino
                 and stat.S_ISDIR(info.st_mode) and found == project
                 and flags & PROJINHERIT and quota and quota['hard'] > 0
                 and quota['soft'] == 0
                 and (not record.get('control_state')
                      or 0 < record.get('minimum_hard', 0) <= quota['hard']))
        if ready and not force_verify:
            self.checkpoint('verified-root', path=root)
            return

        # Persist incomplete before touching a single inode. A killed worker
        # resumes the walk using the same ID, including after final verification.
        record['complete'] = False
        self.registry.save()
        self.checkpoint('tagging', path=root)
        # statvfs(root) may already be clamped by its old quota. Count allocated
        # blocks while writers are held, including foreign IDs awaiting retag.
        tree_bytes = 0
        for number, (_, item) in enumerate(owned_tree(walk_root, roots), 1):
            tree_bytes += item.st_blocks * 512
            if number % 25000 == 0:
                self.checkpoint('inventory', path=root, inodes=number)
        if record.get('control_state') and not record.get('minimum_hard'):
            # Old devices may keep control state on the thin default filesystem.
            # Preserve a fixed small allowance so atomic registry replacement
            # still works when runtime/apps hit their quota. Charge the unused
            # part to the physical ledger on every pass; never lend it twice.
            minimum = max(256 * 1024**2, tree_bytes + 64 * 1024**2)
            minimum = ((minimum + QUANTUM - 1) // QUANTUM) * QUANTUM
            record.update(minimum_hard=minimum, max_hard=minimum)
            self.registry.save()
        temporary_limit = max(QUANTUM, tree_bytes + (quota or {}).get('used', 0) + 64 * 1024**2,
                              record.get('minimum_hard', 0))
        temporary_limit = ((temporary_limit + QUANTUM - 1) // QUANTUM) * QUANTUM
        set_quota(mount, project, temporary_limit)
        if read_quotas(mount).get(project, {}).get('hard') != temporary_limit:
            raise PressureError('temporary migration quota could not be verified')
        count, changed = assign_tree(
            walk_root, project, roots, attributes=self.attributes,
            progress=lambda count, changed: self.checkpoint(
                'tagging', path=root, inodes=count, changed=changed))
        self.checkpoint('flushing', path=root, inodes=count, changed=changed)
        flush_filesystem(root)
        self.checkpoint('verifying', path=root)
        verified = verify_tree(walk_root, project, roots, attributes=self.attributes)
        quota = read_quotas(mount).get(project)
        if quota is None:
            raise PressureError('tagged project missing from quota accounting')
        hard = max(QUANTUM, quota['used'], record.get('minimum_hard', 0))
        set_quota(mount, project, hard)
        final = read_quotas(mount).get(project, {})
        if final.get('hard') != hard or final.get('soft') != 0:
            raise PressureError('migration final quota verification failed')
        record['ownership_version'] = 2
        record['root_inode'] = info.st_ino
        record['inodes'] = verified
        self.registry.complete(identity)
        self.checkpoint('complete', path=root, inodes=verified, changed=changed)
