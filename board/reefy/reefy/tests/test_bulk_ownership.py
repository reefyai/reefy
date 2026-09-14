"""Checkpoint ordering and worker decisions, with kernel operations mocked."""
import _bootstrap  # noqa: F401
import contextlib
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from reefy import bulk_ownership as own, bulk_storage as guardmod
from reefy.bulk_policy import INTERVAL, SAMPLE_MAX_AGE, Pool, StorageError


class CheckpointTests(unittest.TestCase):
    def test_completion_commits_tagging_before_checkpoint(self):
        events = []
        with patch.object(own, 'sync_filesystem', side_effect=lambda fd: events.append('tags')), \
                patch.object(own, 'expected_marker', return_value=b'verified'), \
                patch.object(own.os, 'setxattr', create=True, side_effect=lambda *a: events.append('marker')), \
                patch.object(own.os, 'fsync', side_effect=lambda fd: events.append('durable')):
            own.complete(7, '/synthetic/media', 'filesystem', 42)
        self.assertEqual(events, ['tags', 'marker', 'durable'])

    def test_flush_failure_cannot_publish_success(self):
        with patch.object(own, 'sync_filesystem', side_effect=OSError('flush failed')), \
                patch.object(own.os, 'setxattr', create=True) as write:
            with self.assertRaises(OSError):
                own.complete(7, '/synthetic/media', 'filesystem', 42)
            write.assert_not_called()

    def test_missing_and_unreadable_markers_are_distinct(self):
        with patch.object(own.os, 'getxattr', create=True, side_effect=OSError(errno.ENODATA, 'missing')):
            self.assertIsNone(own.get_marker(7))
        with patch.object(own.os, 'getxattr', create=True, side_effect=OSError(errno.EPERM, 'denied')):
            with self.assertRaises(OSError):
                own.get_marker(7)

    def test_marker_binds_root_filesystem_and_project_not_device_number(self):
        with tempfile.TemporaryDirectory() as directory:
            with own.root_fd(directory) as fd:
                value = own.expected_marker(fd, directory, 'fs-one', 42)
                self.assertNotEqual(value, own.expected_marker(fd, directory, 'fs-two', 42))
                self.assertNotEqual(value, own.expected_marker(fd, directory, 'fs-one', 43))
                self.assertNotEqual(value, own.expected_marker(fd, directory + '-copy', 'fs-one', 42))
                self.assertNotIn(b'dev', value)

    def test_restore_invalidates_before_and_after_but_failure_retains_pending(self):
        events = []
        with patch.object(own, 'ownership_locks', return_value=contextlib.nullcontext()), \
                patch.object(own, 'root_fd', return_value=contextlib.nullcontext(7)), \
                patch.object(own.os, 'setxattr', create=True, side_effect=lambda *a: events.append('pending')), \
                patch.object(own.os, 'fsync', side_effect=lambda *a: events.append('durable')), \
                patch.object(own, 'invalidate_children', side_effect=lambda *a: events.append('clear roots')), \
                patch.object(own, 'invalidate', side_effect=lambda *a: events.append('complete')):
            with own.restore_scope('/synthetic/app'):
                events.append('extract')
        self.assertEqual(events, ['pending', 'durable', 'clear roots', 'extract', 'clear roots', 'complete'])
        events.clear()
        with patch.object(own, 'ownership_locks', return_value=contextlib.nullcontext()), \
                patch.object(own, 'root_fd', return_value=contextlib.nullcontext(7)), \
                patch.object(own.os, 'setxattr', create=True), patch.object(own.os, 'fsync'), \
                patch.object(own, 'invalidate_children'), patch.object(own, 'invalidate') as finish:
            with self.assertRaises(RuntimeError):
                with own.restore_scope('/synthetic/app'):
                    raise RuntimeError('extraction failed')
            finish.assert_not_called()


class GuardTests(unittest.TestCase):
    def make_guard(self, directory):
        with patch.object(guardmod, 'FileAttributes'):
            result = guardmod.Guard(directory, directory)
        result.attributes.read.return_value = [0x200, 0, 0, 42, 0]
        result.registry['filesystems']['fs'] = {'project': 42, 'roots': {'/synthetic/media': [999, 999]}, 'mountpoint': '/synthetic'}
        result.inventory = lambda classes: ({'fs': {'mount': {'target': '/synthetic'}, 'roots': ['/synthetic/media']}}, [])
        result.start_repair = Mock()
        return result

    def test_verified_root_skips_scan_despite_changed_legacy_device_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = self.make_guard(directory)
            with patch.object(own, 'require_not_restoring'), patch.object(own, 'verified', return_value=True), \
                    patch.object(guardmod, 'physical_sample', return_value=Pool(100000000000, 50000000000, 1, 100, 524288, True)), \
                    patch.object(guardmod, 'read_quotas', return_value={42: {'used': 10000, 'hard': 10000}}), \
                    patch.object(guardmod, 'apply_targets'):
                guard.pass_once({'/synthetic/media': 'bulk'})
                guard.pass_once({'/synthetic/media': 'bulk'})
            guard.start_repair.assert_not_called()

    def test_missing_marker_starts_background_repair_without_growth(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = self.make_guard(directory)
            with patch.object(own, 'require_not_restoring'), patch.object(own, 'verified', return_value=False), \
                    patch.object(guardmod, 'physical_sample', return_value=Pool(100000000000, 50000000000, 1, 100, 524288, True)), \
                    patch.object(guardmod, 'read_quotas', return_value={42: {'used': 10000, 'hard': 10000}}), \
                    patch.object(guardmod, 'apply_targets') as apply:
                status = guard.pass_once({'/synthetic/media': 'bulk'})
            guard.start_repair.assert_called_once()
            self.assertEqual(status['stage'], 'degraded')
            self.assertFalse(apply.call_args.kwargs['allow_growth'])

    def test_sixty_second_poll_does_not_extend_sample_freshness(self):
        self.assertEqual((INTERVAL, SAMPLE_MAX_AGE), (60, 10))
        with tempfile.TemporaryDirectory() as directory:
            guard = self.make_guard(directory)
            ticks = iter([100, 111])
            def apply(current, desired, write, read, **kwargs):
                write('fs', current['fs'] + 4096)
            with patch.object(own, 'require_not_restoring'), patch.object(own, 'verified', return_value=True), \
                    patch.object(guardmod, 'physical_sample', return_value=Pool(100000000000, 50000000000, 1, 100, 524288, True)), \
                    patch.object(guardmod, 'read_quotas', return_value={42: {'used': 10000, 'hard': 10000}}), \
                    patch.object(guardmod, 'apply_targets', side_effect=apply), \
                    patch.object(guardmod.time, 'monotonic', side_effect=lambda: next(ticks)), \
                    patch.object(guardmod, 'set_quota') as write:
                with self.assertRaisesRegex(StorageError, 'sample expired'):
                    guard.pass_once({'/synthetic/media': 'bulk'})
            write.assert_not_called()

    def test_mount_filter_ignores_unrelated_but_keeps_mount_identity(self):
        from io import StringIO
        rows = '1 0 8:1 / / rw - xfs /dev/root rw\n2 1 8:2 / /synthetic rw - xfs /dev/data rw\n3 1 0:1 / /docker/merged rw - overlay overlay rw\n'
        with patch('builtins.open', return_value=StringIO(rows)):
            before = guardmod.relevant_mounts(['/synthetic/media'])
        with patch('builtins.open', return_value=StringIO(rows.replace('3 1', '4 1'))):
            self.assertEqual(before, guardmod.relevant_mounts(['/synthetic/media']))
        with patch('builtins.open', return_value=StringIO(rows.replace('2 1', '5 1'))):
            self.assertNotEqual(before, guardmod.relevant_mounts(['/synthetic/media']))


if __name__ == '__main__':
    unittest.main()
