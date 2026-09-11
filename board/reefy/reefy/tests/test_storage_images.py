from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy.storage_images import maintain


class ImageMaintenanceTests(unittest.TestCase):
    def test_reference_renewed_between_observation_and_deletion_resets_age(self):
        image = 'sha256:' + 'a' * 64
        first = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
        now = datetime(2024, 4, 2, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            path.write_text(json.dumps({'version': 1, 'last_observed': first,
                                       'images': {image: {'unreferenced_since': first}}}))
            calls = 0

            def inventory():
                nonlocal calls
                calls += 1
                if calls == 2:
                    # A short install completed between the two locked phases.
                    # It no longer has a current reference but reset eligibility.
                    history = json.loads(path.read_text())
                    history['images'].pop(image)
                    path.write_text(json.dumps(history))
                return {image}, set()

            with patch('reefy.storage_images.image_lock', return_value=nullcontext()), \
                    patch('reefy.storage_images.docker_inventory', side_effect=inventory), \
                    patch('reefy.storage_images.resolve_references', return_value=set()), \
                    patch('reefy.storage_images.command') as command:
                removed = maintain(references=lambda: set(), history_path=str(path),
                                   now=now, clock_trusted=True)
            self.assertEqual(removed, [])
            command.assert_not_called()
            self.assertEqual(json.loads(path.read_text())['images'][image]['unreferenced_since'], now)

    def test_only_expired_unreferenced_identity_is_removed_without_force(self):
        old, referenced, young = ('sha256:' + char * 64 for char in 'abc')
        first = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
        now = datetime(2024, 4, 2, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            path.write_text(json.dumps({'version': 1, 'last_observed': now - 1,
                'images': {old: {'unreferenced_since': first},
                           referenced: {'unreferenced_since': first},
                           young: {'unreferenced_since': now - 100}}}))
            with patch('reefy.storage_images.image_lock', return_value=nullcontext()), \
                    patch('reefy.storage_images.docker_inventory', return_value=({old, referenced, young}, {referenced})), \
                    patch('reefy.storage_images.resolve_references', return_value=set()), \
                    patch('reefy.storage_images.command') as command:
                removed = maintain(references=lambda: set(), history_path=str(path),
                                   now=now, clock_trusted=True)
            self.assertEqual(removed, [old])
            command.assert_called_once_with(['docker', 'image', 'rm', old])


if __name__ == '__main__':
    unittest.main()
