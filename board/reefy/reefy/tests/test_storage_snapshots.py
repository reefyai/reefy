import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy.storage_pressure import PressureError
from reefy.storage_snapshots import backup_snapshots, cleanup_orphans


SNAPSHOT = 'reefy_snap_0123456789ab_1234'
TARGET = '/mnt/reefy-data/snapshots/synthetic/config'


class SnapshotRecoveryTests(unittest.TestCase):
    def test_inventory_does_not_confuse_app_volumes_with_snapshots(self):
        report = {'report': [{'lv': [{'lv_name': SNAPSHOT},
                                    {'lv_name': 'reefy_backup_0123456789ab'},
                                    {'lv_name': 'unrelated'}]}]}
        with patch('reefy.storage_snapshots.command', return_value=json.dumps(report)):
            self.assertEqual(backup_snapshots(), [SNAPSHOT])
        for value in ({}, {'report': []}, {'report': [{'lv': [{'lv_name': 'reefy_snap_unknown'}]}]}):
            with self.subTest(value=value), patch('reefy.storage_snapshots.command', return_value=json.dumps(value)):
                with self.assertRaises(PressureError):
                    backup_snapshots()

    def test_cleanup_unmounts_owned_snapshot_and_verifies_lv_absence(self):
        mounts = json.dumps({'filesystems': [
            {'source': '/dev/mapper/alternate-alias[/]', 'target': TARGET, 'maj:min': '253:8'},
            {'source': '/dev/reefy/app', 'target': '/mnt/reefy-data/apps/synthetic/data', 'maj:min': '253:9'},
        ]})
        with patch('reefy.storage_snapshots.backup_snapshots', side_effect=[[SNAPSHOT], []]), \
                patch('reefy.storage_snapshots.command', side_effect=[mounts, '', '']) as command, \
                patch('reefy.storage_snapshots.Path.rmdir'), \
                patch('reefy.storage_snapshots.os.stat', return_value=SimpleNamespace(st_rdev=os.makedev(253, 8))):
            self.assertEqual(cleanup_orphans(), 1)
        self.assertEqual(command.call_args_list[1].args[0], ['umount', TARGET])
        self.assertEqual(command.call_args_list[2].args[0], ['lvremove', '-f', 'reefy/' + SNAPSHOT])

    def test_cleanup_does_not_hide_failed_removal_or_unexpected_mounts(self):
        for target, failure in ((TARGET, PressureError('device busy')),
                                ('/unrelated', None)):
            mounts = json.dumps({'filesystems': [
                {'source': '/dev/reefy/' + SNAPSHOT, 'target': target, 'maj:min': '253:8'}]})
            with self.subTest(target=target), \
                    patch('reefy.storage_snapshots.backup_snapshots', return_value=[SNAPSHOT]), \
                    patch('reefy.storage_snapshots.command', side_effect=[mounts, failure]) as command, \
                    patch('reefy.storage_snapshots.os.stat', return_value=SimpleNamespace(st_rdev=os.makedev(253, 8))):
                with self.assertRaises(PressureError):
                    cleanup_orphans()
                self.assertFalse(any(call.args[0][0] == 'lvremove'
                                     for call in command.call_args_list))


if __name__ == '__main__':
    unittest.main()
