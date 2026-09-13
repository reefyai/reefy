"""Bulk pressure policy and failure behavior, without Linux mounts."""
import _bootstrap  # noqa: F401
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from reefy.bulk_policy import GB, QUANTUM, Pool, StorageError, apply_targets, targets
from reefy.bulk_storage import Guard, valid_root, volume_classes, walk


def pool(capacity=1000 * GB, used=750 * GB, metadata_used=1):
    return Pool(capacity, used, metadata_used, 100, 512 * 1024, True)


class BudgetTests(unittest.TestCase):
    def test_real_free_space_not_virtual_capacity(self):
        result = targets(pool(), {'media': 600 * GB})
        self.assertLessEqual(result['media'], 650 * GB)
        self.assertGreater(result['media'], 650 * GB - QUANTUM)

    def test_other_growth_lowers_media_below_current_usage(self):
        result = targets(pool(used=900 * GB), {'media': 600 * GB})
        self.assertLessEqual(result['media'], 500 * GB)

    def test_floor_applied_once_across_filesystems(self):
        result = targets(pool(used=999 * GB), {'a': 2 * GB, 'b': 2 * GB})
        self.assertLessEqual(sum(result.values()), GB)
        self.assertTrue(all(v > 0 and v % QUANTUM == 0 for v in result.values()))

    def test_empty_groups_share_spare_growth_not_full_budget_each(self):
        result = targets(pool(used=0), {'a': 0, 'b': 0})
        self.assertEqual(result['a'], result['b'])
        self.assertLessEqual(sum(result.values()), 800 * GB)

    def test_size_matrix_and_no_unlimited_zero(self):
        for capacity in (32 * GB, 128 * GB, 512 * GB, 1000 * GB, 10000 * GB):
            for occupancy in (0, 50, 80, 95, 100):
                sample = pool(capacity, capacity * occupancy // 100)
                usage = {'a': sample.used // 2, 'b': sample.used // 4}
                result = targets(sample, usage)
                self.assertTrue(all(0 < v and v % QUANTUM == 0 for v in result.values()))
                self.assertLessEqual(sum(result.values()), capacity)
        with self.assertRaises(StorageError):
            targets(pool(GB, 0), {'a': 0})

    def test_metadata_pressure_reduces_bulk(self):
        result = targets(pool(used=100 * GB, metadata_used=95), {'a': 20 * GB})
        self.assertLessEqual(result['a'], GB)

    def test_deletion_without_reclaim_does_not_refund_grant(self):
        before = targets(pool(), {'a': 600 * GB})['a']
        after = targets(pool(), {'a': 500 * GB})['a']
        self.assertAlmostEqual(before - after, 100 * GB, delta=QUANTUM)
        reclaimed = targets(pool(used=650 * GB), {'a': 500 * GB})['a']
        self.assertAlmostEqual(reclaimed, before, delta=QUANTUM)

    def test_reductions_verified_before_growth(self):
        current = {'a': 10 * QUANTUM, 'b': 2 * QUANTUM}
        desired = {'a': 4 * QUANTUM, 'b': 8 * QUANTUM}
        events = []
        installed = current.copy()

        def write(key, value):
            events.append(('write', key))
            installed[key] = value

        def read(key):
            events.append(('read', key))
            return installed[key]

        apply_targets(current, desired, write, read)
        self.assertEqual(events, [('write', 'a'), ('read', 'a'), ('write', 'b'), ('read', 'b')])
        events.clear()
        with self.assertRaises(StorageError):
            apply_targets(current, desired, write, lambda key: 0)
        self.assertEqual(events, [('write', 'a')])

    def test_degraded_retains_working_limits_without_new_growth(self):
        write = Mock()
        apply_targets({'a': 4096}, {'a': 8192}, write, Mock(), allow_growth=False)
        write.assert_not_called()


class OwnershipTests(unittest.TestCase):
    def test_legacy_policy_cannot_erase_quotas(self):
        self.assertIsNone(volume_classes({}))
        self.assertIsNone(volume_classes({'schema_version': 2, 'apps': [{}]}))
        self.assertEqual(volume_classes({'schema_version': 2, 'apps': [
            {'volume_storage_classes': {'/example': 'bulk'}}]}), {'/example': 'bulk'})

    def test_symlinks_and_nested_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'link').symlink_to('/tmp')
            (root / 'nested').mkdir()
            paths = [path for path, _ in walk(str(root), set())]
            self.assertEqual(len(paths), 3)
            with self.assertRaises(StorageError):
                list(walk(str(root), {str(root / 'nested')}))
        self.assertFalse(valid_root(None))
        self.assertFalse(valid_root('/mnt/reefy-data/apps/../state'))

    def test_pressure_is_resampled_after_inventory(self):
        events = []
        def sample(**kwargs):
            events.append('sample')
            return pool()
        def inventory(classes):
            events.append('inventory')
            return {}, []
        with tempfile.TemporaryDirectory() as directory, patch(
                'reefy.bulk_storage.FileAttributes'), patch(
                'reefy.bulk_storage.physical_sample', side_effect=sample):
            guard = Guard(directory, directory)
            guard.inventory = inventory
            guard.pass_once({})
        self.assertEqual(events, ['sample', 'inventory', 'sample'])

    def test_sampling_failure_never_changes_quota(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                'reefy.bulk_storage.FileAttributes'), patch(
                'reefy.bulk_storage.physical_sample', side_effect=TimeoutError), patch(
                'reefy.bulk_storage.set_quota') as write:
            guard = Guard(directory, directory)
            with self.assertRaises(TimeoutError):
                guard.pass_once({})
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
