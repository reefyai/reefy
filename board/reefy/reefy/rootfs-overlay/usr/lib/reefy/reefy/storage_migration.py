"""Restartable project migration while affected writers are held at boot.

The caller owns the boot barrier. This worker never stops apps or opens their
startup gate itself, and it never runs as an online pre-tagging background job.
"""
import os
import stat
import time

from reefy.storage_pressure import PressureError, QUANTUM, boundaries
from reefy.storage_quota import (
    FileAttributes, PROJINHERIT, Registry, assign_tree, atomic_json,
    flush_filesystem, mount_info, physical_sample, read_quotas,
    require_enforcement, set_quota, verify_tree, owned_tree, RUN_DIR,
)


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
                or sample.used >= boundaries(sample.capacity).state):
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
            require_enforcement(mount['target'])
            quotas = read_quotas(mount['target'])
            identity, record = self.registry.register(
                root, mount, policies[root], occupied=quotas)
            inventory.append((identity, record))
        for identity, record in inventory:
            self._prepare_one(identity, record, roots, force_verify)
        return {identity: record for identity, record in inventory}

    def _prepare_one(self, identity, record, roots, force_verify):
        root, project, mount = record['path'], record['project'], record['mount']
        info = os.lstat(root)
        flags, _, _, found, _ = self.attributes.read(root)
        quotas = read_quotas(mount)
        quota = quotas.get(project)
        ready = (record.get('complete') and record.get('root_inode') == info.st_ino
                 and stat.S_ISDIR(info.st_mode) and found == project
                 and flags & PROJINHERIT and quota and quota['hard'] > 0
                 and quota['soft'] == 0)
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
        for number, (_, item) in enumerate(owned_tree(root, roots), 1):
            tree_bytes += item.st_blocks * 512
            if number % 25000 == 0:
                self.checkpoint('inventory', path=root, inodes=number)
        temporary_limit = max(QUANTUM, tree_bytes + (quota or {}).get('used', 0) + 64 * 1024**2)
        temporary_limit = ((temporary_limit + QUANTUM - 1) // QUANTUM) * QUANTUM
        set_quota(mount, project, temporary_limit)
        if read_quotas(mount).get(project, {}).get('hard') != temporary_limit:
            raise PressureError('temporary migration quota could not be verified')
        count, changed = assign_tree(
            root, project, roots, attributes=self.attributes,
            progress=lambda count, changed: self.checkpoint(
                'tagging', path=root, inodes=count, changed=changed))
        self.checkpoint('flushing', path=root, inodes=count, changed=changed)
        flush_filesystem(root)
        self.checkpoint('verifying', path=root)
        verified = verify_tree(root, project, roots, attributes=self.attributes)
        quota = read_quotas(mount).get(project)
        if quota is None:
            raise PressureError('tagged project missing from quota accounting')
        hard = max(QUANTUM, quota['used'])
        set_quota(mount, project, hard)
        final = read_quotas(mount).get(project, {})
        if final.get('hard') != hard or final.get('soft') != 0:
            raise PressureError('migration final quota verification failed')
        record['root_inode'] = info.st_ino
        record['inodes'] = verified
        self.registry.complete(identity)
        self.checkpoint('complete', path=root, inodes=verified, changed=changed)
