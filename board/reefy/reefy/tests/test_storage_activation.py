import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401
from reefy.dataplane import DataPlane
from reefy.storage_pressure import PressureError
from reefy.storage_service import validate_activation_layout


class ActivationTests(unittest.TestCase):
    def test_unsupported_layout_does_not_replace_cached_legacy_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'desired-state.json'
            legacy = {'compose': {'services': {}}, 'volume_caps': {'synthetic': 80}}
            path.write_text(json.dumps(legacy))
            plane = DataPlane.__new__(DataPlane)
            plane.DESIRED_STATE_PATH = str(path)
            with patch('reefy.storage_service.read_policy', return_value={}), \
                    patch('reefy.storage_service.requires_activation', return_value=True), \
                    patch('reefy.storage_service.validate_activation_layout', side_effect=PressureError('requires XFS')), \
                    patch('reefy.storage_service.request_activation') as activate:
                self.assertFalse(plane._apply_state({'state': {'storage_pressure_policy': {'version': 1}}}))
                activate.assert_not_called()
            self.assertEqual(json.loads(path.read_text()), legacy)

    def test_unmounted_owned_ext4_volume_is_detected_before_activation(self):
        storage = Mock(STORAGE_POOL='pool', STORAGE_VG='vg')
        storage._lv_metadata_names.return_value = {'pool', 'owned'}
        storage._volume_lv_name.return_value = 'owned'
        storage._fs_type.return_value = 'ext4'
        with patch('reefy.storage_service.mount_info'), \
                patch('reefy.storage_service.Storage', return_value=storage):
            with self.assertRaisesRegex(PressureError, 'separate XFS migration'):
                validate_activation_layout({'/mnt/reefy-data/apps/synthetic/data': 'state'})


if __name__ == '__main__':
    unittest.main()
