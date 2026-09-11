#!/usr/bin/env python3
"""Real XFS syscall/migration probe, run only inside a disposable QEMU guest."""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_migration import Migration
from reefy.storage_pressure import GB, PoolSample
from reefy.storage_quota import (
    FileAttributes, Registry, assign_tree, command, flush_filesystem,
    mount_info, owned_tree, read_quotas, require_enforcement, set_quota, verify_tree,
)


def run():
    results = {}
    with tempfile.TemporaryDirectory(prefix='quota-probe-', dir='/mnt/reefy-data') as work:
        image, mount = Path(work) / 'disk.img', Path(work) / 'xfs'
        with image.open('wb') as stream:
            stream.truncate(2 * 1024**3)
        mount.mkdir()
        command(['mkfs.xfs', '-f', str(image)], timeout=30)
        command(['mount', '-o', 'loop,pquota', str(image), str(mount)], timeout=60)
        try:
            require_enforcement(str(mount))
            attrs = FileAttributes()
            media, config, nested = mount / 'media', mount / 'config', mount / 'media' / 'nested'
            media.mkdir()
            config.mkdir()
            nested.mkdir()
            (media / 'old').write_bytes(b'x' * 4096)
            (media / 'symlink').symlink_to(config)
            os.mkfifo(media / 'fifo')
            command(['mount', '--bind', str(config), str(nested)])
            try:
                before = attrs.read(str(config))
                seen = {path for path, _ in owned_tree(str(media))}
                assert str(nested) not in seen, 'same-filesystem bind mount was traversed'
                assign_tree(str(media), 1024, attributes=attrs)
                assert attrs.read(str(config)) == before, 'followed symlink or bind mount'
                verify_tree(str(media), 1024, attributes=attrs)
            finally:
                command(['umount', str(nested)])
            results['symlinks_fifos_bind_boundaries'] = 'passed'
            assign_tree(str(config), 1025, attributes=attrs)
            set_quota(str(mount), 1024, 8 * 1024**2)
            set_quota(str(mount), 1025, 16 * 1024**2)
            recordings = media / 'recordings'
            recordings.mkdir()
            assert attrs.read(str(recordings))[3] == 1024
            free_before = shutil.disk_usage(recordings).free
            assert 0 < free_before <= 8 * 1024**2
            set_quota(str(mount), 1024, 4 * 1024**2)
            assert shutil.disk_usage(recordings).free < free_before
            try:
                with (recordings / 'fill').open('wb', buffering=0) as stream:
                    while True:
                        stream.write(b'x' * 65536)
            except OSError as error:
                assert error.errno in (errno.ENOSPC, errno.EDQUOT), error
            else:
                raise AssertionError('quota did not stop allocation')
            (config / 'independent').write_bytes(b'committed state')
            quota = read_quotas(str(mount))[1024]
            assert quota['used'] > 0
            set_quota(str(mount), 1024, 1024)
            assert shutil.disk_usage(recordings).free == 0
            assert read_quotas(str(mount))[1024]['used'] >= quota['used']
            (recordings / 'fill').unlink()
            set_quota(str(mount), 1024, 4 * 1024**2)
            (recordings / 'resumed').write_bytes(b'resumed')
            results['live_statfs_quota_errors_and_independent_state'] = 'passed'

            # Exercise the production migration worker on an untagged tree.
            old = mount / 'legacy'
            old.mkdir()
            for number in range(2000):
                (old / str(number)).write_bytes(b'preserved' if number % 100 == 0 else b'')
            registry = Registry(str(Path(work) / 'registry.json'))
            progress = []
            worker = Migration(
                registry=registry, attributes=attrs,
                sample=lambda: PoolSample(32 * GB, GB, 100, 1000, 524288),
                report=progress.append)
            worker.prepare({str(old): 'bulk'})
            project = next(iter(registry.data['projects'].values()))['project']
            assert project not in (1024, 1025), 'existing project collision'
            assert (old / '0').read_bytes() == b'preserved'
            assert verify_tree(str(old), project) == 2001
            progress.clear()
            worker.prepare({str(old): 'bulk'})
            assert [item['phase'] for item in progress] == ['verified-root']
            results['migration_content_collision_and_restart'] = 'passed'
            flush_filesystem(str(mount))
        finally:
            command(['umount', str(mount)], timeout=60)
    print(json.dumps(results, sort_keys=True))


if __name__ == '__main__':
    run()
