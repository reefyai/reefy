"""USB reflash validates replacement without erasing mounted GPT first."""
import subprocess
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
from test_control import _load_control_module


class ReflashGptTests(unittest.TestCase):
    def _reset(self, identities, copy_error=None):
        control = _load_control_module()
        plane = object.__new__(control.ControlPlane)
        plane._storage = mock.Mock()
        plane._storage._find_usb_disk.return_value = '/dev/synthetic-usb'
        plane._storage._find_data_dir.return_value = '/tmp/synthetic-data'
        plane._publish_stage = mock.Mock()
        plane._hard_reboot = mock.Mock()
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == 'dd' and copy_error:
                raise copy_error
            return subprocess.CompletedProcess(args, 0)

        with mock.patch.object(control.os, 'statvfs', return_value=mock.Mock(f_bavail=1000, f_frsize=4096)), mock.patch.object(control.subprocess, 'run', side_effect=run), mock.patch.object(control, '_boot_gpt_identity', side_effect=identities) as read:
            plane._reset_to_bootstrap({'reflash': True, 'image_url': 'https://download.invalid/image'})
        return plane, calls, read

    def test_success_validates_copy_and_never_runs_gpt_tools(self):
        identity = (b'synthetic-guid', b'synthetic-partitions')
        plane, calls, read = self._reset([identity, identity])
        self.assertEqual([args[0] for args, _ in calls], ['curl', 'dd'])
        self.assertEqual(read.call_args_list, [mock.call('/tmp/synthetic-data/reefy-reflash.raw'), mock.call('/dev/synthetic-usb')])
        self.assertTrue(calls[1][1]['check'])
        self.assertNotIn('timeout', calls[1][1])
        self.assertIn('conv=fsync', calls[1][0])
        plane._hard_reboot.assert_called_once()

    def test_invalid_download_never_writes_or_reboots(self):
        plane, calls, _ = self._reset(RuntimeError('invalid GPT'))
        self.assertEqual([args[0] for args, _ in calls], ['curl'])
        plane._hard_reboot.assert_not_called()
        plane._publish_stage.assert_called_with('error', 'Reflash USB write failed')

    def test_copy_failure_never_reboots_or_falls_through_to_wipe(self):
        for error in (subprocess.CalledProcessError(1, ['dd']), subprocess.TimeoutExpired(['dd'], 3600)):
            with self.subTest(error=type(error).__name__):
                plane, calls, read = self._reset([(b'guid', b'entries')], error)
                self.assertEqual([args[0] for args, _ in calls], ['curl', 'dd'])
                read.assert_called_once()
                plane._hard_reboot.assert_not_called()
                plane._publish_stage.assert_called_with('error', 'Reflash USB write failed')

    def test_copy_mismatch_never_reboots(self):
        plane, calls, _ = self._reset([(b'new', b'entries'), (b'old', b'entries')])
        self.assertEqual([args[0] for args, _ in calls], ['curl', 'dd'])
        plane._hard_reboot.assert_not_called()
        plane._publish_stage.assert_called_with('error', 'Reflash USB write failed')
