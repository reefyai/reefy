import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401
from reefy.dataplane import DataPlane
from reefy.storage_pressure import PressureError
from reefy.storage_service import verify_restore


class RestoreOwnershipTests(unittest.TestCase):
    def test_content_marker_does_not_bypass_failed_ownership_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory, 'synthetic', '.restored')
            marker.parent.mkdir()
            marker.write_text('synthetic-archive\n')
            plane = DataPlane.__new__(DataPlane)
            plane.BACKUP_DIR = directory
            plane._publish_restore_status = Mock()
            instance = {'instance_uuid': 'synthetic', 'restore_from': 'synthetic-archive',
                        'paths': ['/mnt/reefy-data/apps/synthetic/config']}
            with patch('reefy.storage_service.verify_restore', side_effect=PressureError('ownership mismatch')):
                self.assertEqual(plane._restore_instances({'instances': [instance]}), {'synthetic'})
            self.assertTrue(marker.exists())
            self.assertEqual(plane._publish_restore_status.call_args.args[1], 'error')

    def repair(self, containers):
        path = '/mnt/reefy-data/apps/synthetic/data'
        record = {'path': path, 'mount': '/mnt/reefy-data', 'project': 1025, 'complete': True}
        registry = Mock(data={'active': True, 'projects': {'synthetic:' + path: record}})
        with patch('reefy.storage_service.Registry', return_value=registry), \
                patch('reefy.storage_service.mount_info', return_value={'uuid': 'synthetic', 'target': '/mnt/reefy-data'}), \
                patch('reefy.storage_service.require_enforcement'), \
                patch('reefy.storage_service.verify_tree', side_effect=[PressureError('mixed ownership'), None]), \
                patch('reefy.storage_service.command', side_effect=['container123', json.dumps(containers)]), \
                patch('reefy.storage_service.assign_tree') as assign, \
                patch('reefy.storage_service.flush_filesystem'), \
                patch('reefy.storage_service.read_quotas', return_value={1025: {'used': 4096, 'hard': 8192, 'soft': 0}}):
            try:
                verify_restore([path], repair=True)
            finally:
                self.assignment = assign

    def test_extracted_metadata_is_repaired_without_changing_destination_quota(self):
        self.repair([{'Mounts': [{'Source': '/mnt/reefy-data/apps/synthetic-source/data'}]}])
        self.assertEqual(self.assignment.call_args.args[1], 1025)

    def test_repair_refuses_a_destination_exposed_through_a_running_parent_bind(self):
        with self.assertRaisesRegex(PressureError, 'still in use'):
            self.repair([{'Mounts': [{'Source': '/mnt/reefy-data/apps/synthetic'}]}])
        self.assignment.assert_not_called()


if __name__ == '__main__':
    unittest.main()
