"""Verify and reclaim backup snapshot resources before releasing their leases.

Cleanup callers must serialize backup execution or hold all app writers during
activation. A process exiting does not remove its persistent thin snapshots.
"""
import json
import os
from pathlib import Path
import re

from reefy.storage_pressure import PressureError
from reefy.storage_quota import command


VG = 'reefy'
MOUNT_ROOT = '/mnt/reefy-data/snapshots'


def backup_snapshots():
    report = json.loads(command(['lvs', '--reportformat', 'json',
                                 '-o', 'lv_name', VG]))
    try:
        rows = report['report'][0]['lv']
        names = [row['lv_name'].strip() for row in rows]
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise PressureError('cannot inventory backup snapshots') from error
    snapshots = [name for name in names if name.startswith('reefy_snap_')]
    if any(not re.fullmatch(r'reefy_snap_[0-9a-f]{12}_[0-9]+', name)
           for name in snapshots):
        raise PressureError('unrecognized backup snapshot ownership')
    return snapshots


def snapshots_released():
    """Unknown or failed inventory raises; it never frees a reservation."""
    return not backup_snapshots()


def cleanup_orphans():
    snapshots = backup_snapshots()
    if not snapshots:
        return 0
    report = json.loads(command(['findmnt', '--json', '--list',
                                 '-o', 'SOURCE,TARGET,MAJ:MIN']))
    mounts = report.get('filesystems')
    if not isinstance(mounts, list):
        raise PressureError('cannot inventory orphan snapshot mounts')
    for snapshot in snapshots:
        try:
            device = os.stat(f'/dev/{VG}/{snapshot}').st_rdev
            device_id = f'{os.major(device)}:{os.minor(device)}'
        except FileNotFoundError:
            # An inactive orphan has no mountable device node. If the alias is
            # unexpectedly missing for an open LV, lvremove refuses removal
            # and the reservation is retained.
            device_id = None
        targets = [row['target'] for row in mounts
                   if device_id is not None and row.get('maj:min') == device_id]
        for target in targets:
            if not target.startswith(MOUNT_ROOT + '/'):
                raise PressureError('snapshot mounted outside the backup namespace')
            command(['umount', target], timeout=15)
        try:
            command(['lvremove', '-f', f'{VG}/{snapshot}'], timeout=15)
        except PressureError as error:
            raise PressureError(f'orphan removal failed after unmounting '
                                f'{len(targets)} matching mount(s): {error}') from error
        # Empty mount directories are not evidence that a thin LV is gone.
        for target in targets:
            for directory in (Path(target), Path(target).parent):
                if str(directory) == MOUNT_ROOT:
                    break
                try:
                    directory.rmdir()
                except OSError:
                    pass
    if not snapshots_released():
        raise PressureError('orphan backup snapshots remain after cleanup')
    return len(snapshots)
