"""Adoption must observe exact kernel partition identity before any data writes."""
import io
import os
import stat
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import _bootstrap  # noqa: F401
from reefy import storage, boot_slots


class PartitionIdentityTests(unittest.TestCase):
    def ready(self, number=3, mode=stat.S_IFBLK, wrong_type=False, **changes):
        sector = changes.pop('sector', 512)
        values = {'parent': '8:16', 'partition': str(number),
                  'start': str(2048 * sector // 512),
                  'size': str(2048 * sector // 512)}
        values.update(changes)
        kind = uuid.UUID('e3c9e316-0b5c-4db8-817d-f92df00215ae' if number == 3
                         else '0fc63daf-8483-4772-8e79-3d69d8477de4').bytes_le
        if wrong_type: kind = bytes(16)
        def read(path, *args, **kwargs):
            name = 'parent' if str(path).endswith('/dev') else str(path).rsplit('/', 1)[-1]
            return io.StringIO(values[name])
        def device(path):
            return SimpleNamespace(st_mode=mode,
                st_rdev=os.makedev(8, 16 if path == '/dev/synthetic' else 16 + number))
        with patch.object(boot_slots, 'layout', return_value=(sector, 99999,
                {number: {'start': 2048, 'end': 4095, 'type': kind}})), \
                patch.object(storage.os, 'stat', side_effect=device), \
                patch.object(storage.os.path, 'realpath', return_value='/sys/fake/disk/part'), \
                patch('builtins.open', side_effect=read):
            return storage._created_partition_ready('/dev/synthetic', number)

    def test_exact_parent_number_start_size_and_sector_units(self):
        for number in (3, 4):
            for sector in (512, 4096):
                with self.subTest(number=number, sector=sector):
                    self.assertTrue(self.ready(number, sector=sector))

    def test_sibling_stale_truncated_or_malformed_node_is_not_writable(self):
        for changes in ({'parent':'8:32'}, {'partition':'4'}, {'start':'4096'},
                        {'size':'1024'}, {'start':'unknown'}):
            with self.subTest(changes=changes): self.assertFalse(self.ready(**changes))

    def test_regular_file_and_wrong_partition_type_are_not_writable(self):
        self.assertFalse(self.ready(mode=stat.S_IFREG))
        self.assertFalse(self.ready(wrong_type=True))

    def test_bad_gpt_and_missing_node_fail_closed(self):
        for error in (RuntimeError('bad CRC'), OSError('missing node')):
            with patch.object(boot_slots, 'layout', side_effect=error):
                self.assertFalse(storage._created_partition_ready('/dev/synthetic', 3))


class AdoptionRefreshTests(unittest.TestCase):
    def test_key_refresh_timeout_with_verified_kernel_node_allows_restore(self):
        instance = storage.Storage(); calls = []; exists = [False]
        def path_exists(path):
            if path == '/dev/synthetic3':
                if not exists[0]: exists[0] = True; return False
                return True
            return False
        def run(command, **kwargs):
            calls.append(command)
            if command[0] in ('partprobe', 'partx'):
                raise subprocess.TimeoutExpired(command, 10)
            out = 'tmpfs' if command[0] == 'findmnt' else ''
            return SimpleNamespace(returncode=0, stdout=out, stderr='')
        with patch.object(storage.subprocess, 'run', side_effect=run), \
                patch.object(storage.os.path, 'exists', side_effect=path_exists), \
                patch.object(instance, '_find_reefy_disk', return_value=('/dev/synthetic', None)), \
                patch.object(boot_slots, 'next_partition_start', return_value=2048), \
                patch.object(storage, '_repair_boot_gpt'), \
                patch.object(storage, '_created_partition_ready', return_value=True, create=True), \
                patch.object(storage, '_scan_existing_volumes'), \
                patch.object(instance, '_active_reefy_lv_path', return_value='/dev/mapper/synthetic-existing'), \
                patch.object(instance, '_finalize_data_mount', return_value=True) as mount, \
                patch.object(instance, '_write_fresh_keyfile') as key, \
                patch.object(instance, '_provision_luks_stack') as provision:
            instance._ensure_persistent_storage()
            mount.assert_called_once(); key.assert_not_called(); provision.assert_not_called()
        self.assertEqual(sum(c[0] == 'sgdisk' for c in calls), 1)
        self.assertFalse(any(c[0] == 'partprobe' for c in calls))
        self.assertIn(['partx', '--add', '--nr', '3', '/dev/synthetic'], calls)

    def test_unknown_refresh_stops_without_reissuing_or_writing(self):
        for number in (3, 4):
            clock = [0.0]
            with patch.object(storage.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['partx'], 10)) as run, \
                    patch.object(storage, '_created_partition_ready', return_value=False), \
                    patch.object(storage, 'time', SimpleNamespace(
                        monotonic=lambda: clock[0],
                        sleep=lambda duration: clock.__setitem__(0, clock[0]+duration))):
                with self.assertRaisesRegex(RuntimeError, 'provisioning stopped'):
                    storage._refresh_created_partition('/dev/synthetic', number)
                self.assertEqual(run.call_count, 1)
                self.assertLess(clock[0], 1.2)

    def test_nonzero_refresh_requires_positive_kernel_proof(self):
        with patch.object(storage.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), \
                patch.object(storage, '_created_partition_ready', return_value=True):
            storage._refresh_created_partition('/dev/synthetic', 4)

    def test_boot_slot_is_never_a_refresh_target(self):
        with patch.object(storage.subprocess, 'run') as run:
            with self.assertRaises(ValueError): storage._refresh_created_partition('/dev/synthetic', 1)
            run.assert_not_called()


if __name__ == '__main__': unittest.main()
