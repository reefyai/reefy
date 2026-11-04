"""Slot sizing refusals and preservation precede any destructive operation."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _bootstrap
from reefy import boot_slots as slots


class SlotTests(unittest.TestCase):
    def geometry(self, mib=1024, extra=None):
        partitions = {1: {'name': 'reefy-a', 'type': slots.ESP_TYPE,
                          'start': 2048, 'end': 2048 + mib * 2048 - 1}}
        partitions.update(extra or {})
        return 512, 16 * 1024 * 2048 - 34, partitions

    def test_next_partition_start_uses_installed_geometry(self):
        for mib in (512, 1024, 2048):
            with self.subTest(mib=mib):
                geo = self.geometry(mib, {2: {
                    'name': 'reefy-b', 'type': slots.ESP_TYPE,
                    'start': (mib + 1) * 2048, 'end': (2 * mib + 1) * 2048 - 1}})
                with mock.patch.object(slots, 'layout', return_value=geo):
                    self.assertEqual(slots.next_partition_start('synthetic', (1, 2)),
                                     (2 * mib + 1) * 2048)

    def test_existing_b_is_never_reformatted_or_copied(self):
        geo = self.geometry(extra={2: {'name': 'reefy-b', 'type': slots.ESP_TYPE}})
        with mock.patch.object(slots, 'layout', return_value=geo), \
                mock.patch.object(slots, 'run') as run, \
                mock.patch.object(slots, 'manifest') as manifest:
            slots.ensure_slots('synthetic')
            run.assert_not_called()
            manifest.assert_not_called()

    def test_conflicting_partitions_and_small_disk_refuse_before_writes(self):
        layouts = [self.geometry(extra={2: {'name': 'unrelated', 'type': bytes(16)}}),
                   self.geometry(extra={3: {'start': 1048576, 'end': 16777216}})]
        sector, _, partitions = self.geometry()
        layouts.append((sector, partitions[1]['end'] + 2048, partitions))
        for geo in layouts:
            with self.subTest(geometry=geo), \
                    mock.patch.object(slots, 'layout', return_value=geo), \
                    mock.patch.object(slots, 'run') as run, \
                    mock.patch.object(slots, '_repair_boot_gpt') as repair:
                with self.assertRaises(RuntimeError):
                    slots.ensure_slots('synthetic')
                run.assert_not_called()
                repair.assert_not_called()

    def test_capacity_check_accounts_for_filesystem_overhead(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / 'synthetic.efi'
            payload.write_bytes(b'MZ')
            with mock.patch.object(slots, 'run', side_effect=[str(16 * slots.MIB), str(__import__('uuid').UUID(bytes_le=slots.ESP_TYPE))]):
                with self.assertRaises(RuntimeError):
                    slots.check_fit(payload, 'synthetic')
            with mock.patch.object(slots, 'run', side_effect=[str(512 * slots.MIB), str(__import__('uuid').UUID(bytes_le=slots.ESP_TYPE))]):
                slots.check_fit(payload, 'synthetic')
