"""Late disks, missing VG members and refusal without changing boot metadata."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy import storage_discovery as discovery


class DiscoveryTests(unittest.TestCase):
    def exercise(self, arrivals=(0,), members=1, wrong_key=False, usb=False, corrupt=False, timeout_command=None, persistent_timeout=False, timeout_path=None):
        clock = [0.0]
        opened, commands = set(), []
        timed_out = [False]
        paths = ['/dev/data-a', '/dev/data-b'][:members]
        if usb:
            paths = ['/dev/boot4']
        key = Path('/dev/boot3')

        def result(args, **kwargs):
            commands.append(args)
            command = args[1] if args[0] == 'cryptsetup' else args[0]
            if (command == timeout_command and (timeout_path is None or args[-1] == timeout_path)
                    and (persistent_timeout or not timed_out[0])):
                timed_out[0] = True
                clock[0] += kwargs.get('timeout', 5)
                raise subprocess.TimeoutExpired(args, kwargs.get('timeout', 5))
            code, out = 0, ''
            if args[0] == 'lsblk':
                visible = [path for index, path in enumerate(paths) if clock[0] >= arrivals[index]]
                out = '/dev/boot disk\n' + '\n'.join(path + (' part' if usb else ' disk') for path in visible)
                if wrong_key:
                    out += '\n/dev/unrelated disk'
            elif args[:2] == ['cryptsetup', 'luksUUID']:
                out = 'synthetic-' + Path(args[-1]).name
            elif args[:2] == ['cryptsetup', 'luksOpen']:
                if args[2] == '/dev/unrelated':
                    code = 2
                else:
                    opened.add(args[2])
            elif args[0] == 'pvs':
                rows = [] if corrupt else [{'pv_name': path, 'pv_missing': '' if path in opened else 'missing'} for path in paths]
                out = json.dumps({'report': [{'pv': rows}]})
            elif args[0] == 'blkid':
                out = 'ext4'
            return SimpleNamespace(returncode=code, stdout=out)

        with patch.object(discovery, 'key_partition', return_value=('/dev/boot', key)), \
                patch.object(Path, 'is_block_device', return_value=True), \
                patch.object(Path, 'exists', return_value=False), \
                patch.object(discovery, 'run', side_effect=result):
            try:
                discovery.wait('/dev/boot1', now=lambda: clock[0],
                               sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
            except TimeoutError:
                return clock[0], commands, False
        return clock[0], commands, True

    def test_transient_read_only_probe_timeouts_do_not_abandon_existing_storage(self):
        for command in ('lsblk', 'luksUUID', 'luksOpen', 'vgscan', 'pvs'):
            with self.subTest(command=command):
                elapsed, commands, ready = self.exercise(timeout_command=command)
                self.assertTrue(ready)
                self.assertGreaterEqual(elapsed, 5)
                self.assertLess(elapsed, discovery.DEADLINE)
                self.assertFalse(any(command[0] in ('vgchange', 'mkfs', 'xfs_repair', 'mount')
                                     for command in commands))

    def test_unrelated_slow_probe_does_not_hide_complete_owned_storage(self):
        elapsed, commands, ready = self.exercise(
            wrong_key=True, timeout_command='luksUUID', persistent_timeout=True,
            timeout_path='/dev/unrelated')
        self.assertTrue(ready)
        self.assertEqual(elapsed, 5)
        self.assertFalse(any(args[:3] == ['cryptsetup', 'luksOpen', '/dev/unrelated']
                             for args in commands))

    def test_timed_out_vg_refresh_is_retried_before_membership_inspection(self):
        _, commands, ready = self.exercise(timeout_command='vgscan')
        self.assertTrue(ready)
        scans = [index for index, args in enumerate(commands) if args[0] == 'vgscan']
        membership = [index for index, args in enumerate(commands) if args[0] == 'pvs']
        self.assertEqual(len(scans), 2)
        self.assertGreater(min(membership), max(scans))
        self.assertEqual(sum(args[:2] == ['cryptsetup', 'luksOpen'] for args in commands), 1)

    def test_missing_member_keeps_full_deadline_after_transient_probe_timeout(self):
        elapsed, commands, ready = self.exercise(
            arrivals=(0, 1000), members=2, timeout_command='luksUUID')
        self.assertFalse(ready)
        self.assertEqual(elapsed, 900)
        self.assertFalse(any('--partial' in command for command in commands))

    def test_repeated_probe_timeouts_remain_bounded_by_original_deadline(self):
        elapsed, commands, ready = self.exercise(
            timeout_command='luksUUID', persistent_timeout=True)
        self.assertFalse(ready)
        self.assertEqual(elapsed, 900)
        self.assertFalse(any(command[0] in ('vgchange', 'mkfs', 'xfs_repair', 'mount')
                             for command in commands))

    def test_fresh_image_skips_wait_and_all_storage_commands(self):
        with patch.object(discovery, 'key_partition', return_value=('/dev/boot', Path('/dev/boot3'))), \
                patch.object(Path, 'is_block_device', return_value=False), \
                patch.object(discovery, 'run') as run:
            discovery.wait('/dev/boot1')
        run.assert_not_called()

    def test_existing_single_disk_releases_immediately(self):
        elapsed, commands, ready = self.exercise()
        self.assertTrue(ready)
        self.assertEqual(elapsed, 0)
        self.assertEqual(sum(command[:2] == ['cryptsetup', 'luksOpen'] for command in commands), 1)

    def test_single_disk_arriving_late_is_recovered(self):
        elapsed, _, ready = self.exercise(arrivals=(4,))
        self.assertTrue(ready)
        self.assertEqual(elapsed, 4)

    def test_both_members_are_required_and_opened_only_once(self):
        elapsed, commands, ready = self.exercise(arrivals=(1, 7), members=2)
        self.assertTrue(ready)
        self.assertEqual(elapsed, 7)
        self.assertEqual(sum(command[:2] == ['cryptsetup', 'luksOpen'] for command in commands), 2)
        self.assertFalse(any(command[0] in ('vgchange', 'mkfs', 'xfs_repair', 'mount') for command in commands))

    def test_missing_member_waits_real_policy_without_partial_activation(self):
        self.assertEqual(discovery.DEADLINE, 900)
        elapsed, commands, ready = self.exercise(arrivals=(0, 1000), members=2)
        self.assertFalse(ready)
        self.assertEqual(elapsed, 900)
        self.assertFalse(any('--partial' in command for command in commands))

    def test_no_disks_with_existing_key_also_times_out(self):
        elapsed, _, ready = self.exercise(arrivals=(1000,))
        self.assertFalse(ready)
        self.assertEqual(elapsed, 900)

    def test_unrelated_encrypted_disk_does_not_release_wait_or_repeat_key_attempt(self):
        elapsed, commands, ready = self.exercise(arrivals=(4,), wrong_key=True)
        self.assertTrue(ready)
        self.assertEqual(elapsed, 4)
        unrelated = [command for command in commands if command[:3] == ['cryptsetup', 'luksOpen', '/dev/unrelated']]
        self.assertEqual(len(unrelated), 1)

    def test_legacy_direct_usb_filesystem_remains_supported(self):
        elapsed, commands, ready = self.exercise(usb=True)
        self.assertTrue(ready)
        self.assertEqual(elapsed, 0)
        self.assertTrue(any(command[:4] == ['cryptsetup', 'luksOpen', '/dev/boot4', 'reefy-data'] for command in commands))

    def test_unreadable_outer_metadata_reaches_existing_recovery_once(self):
        elapsed, commands, ready = self.exercise(corrupt=True)
        self.assertTrue(ready)
        self.assertEqual(elapsed, 0)
        self.assertEqual(sum(command[0] == 'pvs' for command in commands), 1)
        self.assertFalse(any('repair' in ' '.join(command) for command in commands))

    def test_transient_open_error_rechecks_completed_mapping(self):
        clock = [0.0]
        created = [False]
        attempts = []

        def result(args, **kwargs):
            code, out = 0, ''
            if args[0] == 'lsblk':
                out = '/dev/boot disk\n/dev/data-a disk\n'
            elif args[:2] == ['cryptsetup', 'luksUUID']:
                out = 'synthetic-luks-id'
            elif args[:2] == ['cryptsetup', 'luksOpen']:
                attempts.append(args)
                created[0] = True
                code = 5
            elif args[0] == 'pvs':
                out = json.dumps({'report': [{'pv': [{'pv_missing': ''}]}]})
            return SimpleNamespace(returncode=code, stdout=out)

        with patch.object(discovery, 'key_partition', return_value=('/dev/boot', Path('/dev/boot3'))), \
                patch.object(Path, 'is_block_device', return_value=True), \
                patch.object(Path, 'exists', side_effect=lambda: created[0]), \
                patch.object(discovery, 'run', side_effect=result):
            discovery.wait('/dev/boot1', now=lambda: clock[0],
                           sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(clock[0], 0.25)

    def test_missing_pv_report_with_nonzero_status_is_not_ready(self):
        report = json.dumps({'report': [{'pv': [{'pv_missing': 'missing'}]}]})
        with patch.object(discovery, 'run', return_value=SimpleNamespace(returncode=5, stdout=report)):
            self.assertFalse(discovery.complete_vg(5))

    def test_bad_membership_report_is_not_treated_as_complete(self):
        with patch.object(discovery, 'run', return_value=SimpleNamespace(returncode=0, stdout='broken')):
            with self.assertRaises(ValueError):
                discovery.complete_vg(5)

    def test_digit_ending_boot_disk_uses_partition_separator(self):
        with patch.object(discovery, 'run', return_value=SimpleNamespace(returncode=0, stdout='nvme0n1\n')):
            disk, key = discovery.key_partition('/dev/nvme0n1p2')
        self.assertEqual((disk, key), ('/dev/nvme0n1', Path('/dev/nvme0n1p3')))

    def test_timeout_sets_existing_provisioning_guard_only_in_ram(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = Path(directory) / 'run/storage-recovery-failed'
            with patch.object(discovery, 'GUARD', guard), \
                    patch.object(discovery.sys, 'argv', ['storage_discovery', '/dev/boot1']), \
                    patch.object(discovery, 'wait', side_effect=TimeoutError('900 seconds')):
                self.assertEqual(discovery.main(), 1)
            self.assertIn('repair storage before provisioning', guard.read_text())

    def test_failed_mount_cannot_fall_through_to_destructive_provisioning(self):
        with patch.object(discovery, 'key_partition', return_value=('/dev/boot', Path('/dev/boot3'))), \
                patch.object(Path, 'is_block_device', return_value=True), \
                patch.object(discovery, 'run', return_value=SimpleNamespace(returncode=1, stdout='')):
            with self.assertRaisesRegex(RuntimeError, 'provisioning refused'):
                discovery.verify_mount('/dev/boot1')


if __name__ == '__main__':
    unittest.main()
