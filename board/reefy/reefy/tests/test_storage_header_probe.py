"""Restore discovery tolerates bounded I/O delay without reprovisioning."""
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy import storage


class HeaderProbeTests(unittest.TestCase):
    def exercise(self, duration=0, returncode=0, logger=None):
        instance = storage.Storage()
        clock, commands, messages = [0.0], [], []

        def run(command, **kwargs):
            commands.append((command, kwargs))
            output = ''
            if command[0] == 'findmnt':
                output = 'tmpfs'
            elif command[:3] == ['lsblk', '-no', 'PKNAME']:
                output = 'synthetic-boot'
            elif command[:3] == ['lsblk', '-dpno', 'NAME']:
                output = '/dev/synthetic-boot\n/dev/synthetic-data'
            elif command[:2] == ['cryptsetup', 'isLuks']:
                budget = kwargs['timeout']
                clock[0] += min(duration, budget)
                if duration > budget:
                    raise subprocess.TimeoutExpired(command, budget)
                return SimpleNamespace(returncode=returncode, stdout='', stderr='')
            return SimpleNamespace(returncode=0, stdout=output, stderr='')

        with patch.object(storage.subprocess, 'run', side_effect=run), \
                patch.object(storage.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(storage.os.path, 'exists',
                             side_effect=lambda path: not path.startswith('/dev/mapper/')), \
                patch.object(storage, '_scan_existing_volumes') as scan, \
                patch.object(instance, '_find_reefy_disk',
                             return_value=('/dev/synthetic-boot', '/dev/synthetic-boot1')), \
                patch.object(instance, '_active_reefy_lv_path',
                             return_value='/dev/synthetic-vg/data'), \
                patch.object(instance, '_finalize_data_mount', return_value=True) as mount, \
                patch.object(instance, '_write_fresh_keyfile') as key, \
                patch.object(instance, '_setup_internal_persistent') as provision:
            error = None
            try:
                instance._ensure_persistent_storage(
                    {'devices': ['synthetic-data']}, logger or messages.append)
            except subprocess.TimeoutExpired as caught:
                error = caught
            key.assert_not_called()
            provision.assert_not_called()
            if error:
                scan.assert_not_called()
                mount.assert_not_called()
            else:
                mount.assert_called_once()
        return commands, messages, error

    def test_seven_second_existing_header_is_opened_without_reprovisioning(self):
        commands, messages, error = self.exercise(duration=7)
        self.assertIsNone(error)
        self.assertEqual(sum(c[:2] == ['cryptsetup', 'isLuks'] for c, _ in commands), 1)
        self.assertTrue(any(c[:2] == ['cryptsetup', 'luksOpen'] for c, _ in commands))
        self.assertTrue(any('LUKS header probe elapsed=7.000s budget=30s' in m for m in messages))

    def test_permanent_stall_stops_at_thirty_seconds_without_retry_or_mutation(self):
        commands, messages, error = self.exercise(duration=120)
        self.assertIsInstance(error, subprocess.TimeoutExpired)
        self.assertEqual(error.timeout, 30)
        self.assertEqual(sum(c[:2] == ['cryptsetup', 'isLuks'] for c, _ in commands), 1)
        self.assertFalse(any(c[:2] == ['cryptsetup', 'luksOpen'] for c, _ in commands))
        self.assertTrue(any('outcome=timeout' in m for m in messages))

    def test_fast_non_luks_result_keeps_existing_skip_behavior(self):
        commands, messages, error = self.exercise(returncode=1)
        self.assertIsNone(error)
        self.assertFalse(any(c[:2] == ['cryptsetup', 'luksOpen'] for c, _ in commands))
        self.assertFalse(any('LUKS header probe elapsed=' in m for m in messages))

    def test_diagnostic_failure_does_not_change_probe_result(self):
        def logger(message):
            if 'LUKS header probe elapsed=' in message:
                raise OSError('synthetic closed diagnostic stream')
        _, _, error = self.exercise(duration=7, logger=logger)
        self.assertIsNone(error)


if __name__ == '__main__':
    unittest.main()
