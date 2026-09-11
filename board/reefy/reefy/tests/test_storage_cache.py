from contextlib import nullcontext
import json
import unittest
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401
from reefy.storage_cache import preserve_new_mounts
from reefy.storage_pressure import PressureError


class CacheMigrationTests(unittest.TestCase):
    def test_pending_segments_are_copied_only_after_stopping_old_writer(self):
        source = '/mnt/reefy-data/apps/synthetic/cache'
        record = {'path': source, 'project': 1024, 'complete': True, 'storage_class': 'bulk'}
        registry = Mock(data={'active': True, 'projects': {'volume': record}})
        container = {'Id': 'synthetic-container', 'Config': {'Labels': {
            'com.docker.compose.service': 'recorder'}}, 'Mounts': [], 'SizeRw': 8192}
        calls = []

        def command(args, **kwargs):
            calls.append(args)
            if args[1] == 'ps':
                return 'synthetic-container'
            if args[1] == 'inspect':
                return json.dumps([container])
            return ''

        with patch('reefy.storage_cache.Registry', return_value=registry), \
                patch('reefy.storage_cache.command', side_effect=command), \
                patch('reefy.storage_cache.reservation', return_value=nullcontext()) as reserve, \
                patch('reefy.storage_cache.verify_tree') as verify:
            preserve_new_mounts({'services': {'recorder': {
                'volumes': [source + ':/tmp/cache']}}}, 'synthetic-project')
        actions = [args[1] for args in calls]
        self.assertLess(actions.index('update'), actions.index('stop'))
        self.assertLess(actions.index('stop'), actions.index('cp'))
        self.assertNotIn('rm', actions)
        self.assertIn('--restart=no', calls[actions.index('update')])
        self.assertEqual(reserve.call_args.kwargs, {'storage_class': 'bulk', 'target': 'volume'})
        verify.assert_called_once_with(source, 1024)

    def test_partial_copy_failure_keeps_source_and_blocks_startup(self):
        source = '/mnt/reefy-data/apps/synthetic/cache'
        registry = Mock(data={'active': True, 'projects': {'volume': {
            'path': source, 'project': 1024, 'complete': True, 'storage_class': 'bulk'}}})
        container = {'Id': 'synthetic-container', 'Config': {'Labels': {
            'com.docker.compose.service': 'recorder'}}, 'Mounts': [], 'SizeRw': 8192}

        def command(args, **kwargs):
            if args[1] == 'ps': return 'synthetic-container'
            if args[1] == 'inspect': return json.dumps([container])
            if args[1] == 'cp': raise PressureError('disk quota exceeded')
            self.assertIn(args[1], ('update', 'stop'))
            return ''

        with patch('reefy.storage_cache.Registry', return_value=registry), \
                patch('reefy.storage_cache.command', side_effect=command), \
                patch('reefy.storage_cache.reservation', return_value=nullcontext()), \
                patch('reefy.storage_cache.verify_tree') as verify:
            with self.assertRaisesRegex(PressureError, 'quota exceeded'):
                preserve_new_mounts({'services': {'recorder': {
                    'volumes': [source + ':/tmp/cache']}}}, 'synthetic-project')
            verify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
