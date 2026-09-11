import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import _bootstrap  # noqa: F401
from reefy.storage_runtime import LAYER_INITIAL_SIZE, configure_daemon, active_native_projects


class RuntimeTests(unittest.TestCase):
    def test_quota_options_only_on_activated_xfs_boots(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = (Path(directory) / name for name in ('source', 'generated'))
            config = {'data-root': '/synthetic/docker', 'log-driver': 'json-file',
                      'log-opts': {'max-size': '20m', 'max-file': '3'}}
            source.write_text(json.dumps(config))
            configure_daemon(False, source=source, destination=str(destination))
            self.assertEqual(json.loads(destination.read_text()), config)
            configure_daemon(True, source=source, destination=str(destination))
            active = json.loads(destination.read_text())
            self.assertEqual(active['storage-opts'], [f'overlay2.size={LAYER_INITIAL_SIZE}'])
            self.assertEqual(active['storage-driver'], 'overlay2')
            self.assertEqual(active['log-opts'], config['log-opts'])
            self.assertEqual(json.loads(source.read_text()), config)
            configure_daemon(False, source=source, destination=str(destination))
            self.assertNotIn('storage-opts', json.loads(destination.read_text()))

    def test_native_inventory_is_shallow_and_excludes_deleted_or_symlinked_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'active').mkdir()
            (root / 'image').mkdir()
            (root / 'active/nested').mkdir()
            (root / 'alias').symlink_to(root / 'active', target_is_directory=True)
            attributes = Mock()
            attributes.read.side_effect = lambda path: (0, 0, 0, 2**20 + (2 if Path(path).name == 'active' else 0), 0)
            registry = Mock(data={'docker': {'base_project': 2**20}})
            self.assertEqual(active_native_projects(registry, attributes=attributes, overlay=directory), {2**20 + 2})
            self.assertEqual(attributes.read.call_count, 2)


if __name__ == '__main__':
    unittest.main()
