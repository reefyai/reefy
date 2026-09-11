import json
import os
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy.storage_quota import (
    Registry, check_hardlinks, owned_tree, read_quotas, require_enforcement, set_quota,
)
from reefy.storage_pressure import PressureError


class OwnershipTests(unittest.TestCase):
    def test_restart_reuses_destination_identity_and_source_is_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/registry.json'
            mount = {'uuid': 'synthetic-fs', 'target': '/test', 'maj:min': '253:7'}
            registry = Registry(path)
            source, a = registry.register('/test/original/data', mount, 'state', [1024])
            target, b = registry.register('/test/restored/data', mount, 'state')
            self.assertNotEqual(a['project'], b['project'])
            self.assertNotEqual(a['project'], 1024)
            restarted = Registry(path)
            _, again = restarted.register('/test/restored/data', mount, 'state')
            self.assertEqual(again['project'], b['project'])
            self.assertFalse(again['complete'])
            restarted.complete(target)
            self.assertTrue(Registry(path).data['projects'][target]['complete'])
            self.assertEqual(Registry(path).data['projects'][source]['project'], a['project'])

    def test_walk_excludes_other_project_and_never_follows_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            os.mkdir(directory + '/owned')
            os.mkdir(directory + '/other')
            os.symlink(directory + '/other', directory + '/owned/link')
            paths = [p for p, _ in owned_tree(directory, [directory + '/other'])]
            self.assertIn(directory + '/owned/link', paths)
            self.assertNotIn(directory + '/other', paths)

    def test_hardlink_outside_volume_fails_without_mutating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            os.mkdir(directory + '/owned')
            with open(directory + '/owned/file', 'w') as stream:
                stream.write('synthetic content')
            os.link(directory + '/owned/file', directory + '/outside')
            with self.assertRaises(PressureError):
                check_hardlinks(directory + '/owned')
            check_hardlinks(directory)  # both names are in this quota domain

    def test_malformed_registry_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/registry.json'
            with open(path, 'w') as stream:
                json.dump({'version': 99}, stream)
            with self.assertRaises(PressureError):
                Registry(path)


class ToolParsingTests(unittest.TestCase):
    def test_limit_uses_kib_not_xfs_basic_block_suffix(self):
        with patch('reefy.storage_quota.command') as run:
            set_quota('/test', 1024, 8 * 1024**2)
        self.assertIn('bhard=8192k', run.call_args.args[0][3])

    def test_report_uses_exact_units_and_retains_zero_hard_as_unlimited(self):
        with patch('reefy.storage_quota.command', return_value=(
                '#1024 123 0 456 00 [--------]\n#1025 789 0 0 00 [--------]\n')):
            data = read_quotas('/test')
        self.assertEqual(data[1024], {'used': 123*1024, 'soft': 0, 'hard': 456*1024})
        self.assertEqual(data[1025]['hard'], 0)

    def test_accounting_alone_is_not_enforcement(self):
        with patch('reefy.storage_quota.command', return_value='Accounting: ON\nEnforcement: OFF'):
            with self.assertRaises(PressureError):
                require_enforcement('/test')


if __name__ == '__main__':
    unittest.main()
