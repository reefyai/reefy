"""Host policies use synthetic sysfs fixtures, never the host's real devices."""
import functools
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401
from reefy.policies import apply_apst, apply_policies, _supports_apst
from reefy.apply_results import ApplyResultStore
from test_control import _load_control_module


class PolicyTests(unittest.TestCase):
    def test_independent_failures_and_unknown_key(self):
        bad = Mock(side_effect=ValueError('bad value'))
        good = Mock()
        warnings = apply_policies({'bad': 'x', 'good': True, 'unknown': 'secret'},
                                  {('bad',): bad, ('good',): good})
        good.assert_called_once_with(True)
        self.assertEqual(len(warnings), 2)
        self.assertEqual(warnings[-1]['subject']['id'], 'host.policies.bad')
        self.assertNotIn('secret', str(warnings))

    def test_malformed_branch_does_not_trigger_removal(self):
        handler = Mock()
        for value in (None, [], 'script', {'hardware': []}):
            self.assertTrue(apply_policies(value, {('hardware', 'nvme', 'apst'): handler}))
        handler.assert_not_called()

    def test_omission_dispatches_cleanup(self):
        handler = Mock()
        self.assertEqual(apply_policies({}, {('a',): handler}), [])
        handler.assert_called_once_with(None)

    def test_policy_warning_survives_status_sanitizer(self):
        warning = {'code': 'policy.apply_failed', 'message': 'cannot write /sys/example', 'subject': {'kind': 'policy', 'id': 'host.policies.hardware.nvme.apst'}, 'extra': 'discard'}
        warnings = ApplyResultStore._sanitize_warnings([warning])
        self.assertEqual(warnings[0]['message'], 'cannot write [PATH]')
        self.assertNotIn('extra', warnings[0])
        self.assertIn('host.policies.hardware.nvme.apst', _load_control_module().ControlPlane._ready_stage_message(warnings))


class ApstTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.controllers = root / 'nvme'
        self.controllers.mkdir()
        self.default = root / 'default'
        self.default.write_text('100000\n')
        self.managed = root / 'managed'
        self.apply = functools.partial(apply_apst, controllers=self.controllers,
                                       default_latency=self.default, managed=self.managed)
        self.qos = self.add_controller('nvme0')

    def add_controller(self, name):
        qos = self.controllers / name / 'power/pm_qos_latency_tolerance_us'
        qos.parent.mkdir(parents=True)
        qos.write_text('100000\n')
        return qos

    def test_disabled_replay_new_controller_and_removal(self):
        self.apply('disabled')
        self.assertEqual(self.qos.read_text().strip(), '0')
        second = self.add_controller('nvme1')
        self.apply('disabled')
        self.assertEqual(second.read_text().strip(), '0')
        self.apply(None)
        self.assertEqual(self.qos.read_text().strip(), '100000')
        self.assertEqual(second.read_text().strip(), '100000')
        self.assertEqual(list(self.managed.iterdir()), [])

    def test_omission_leaves_unmanaged_override(self):
        self.qos.write_text('123\n')
        self.apply(None)
        self.assertEqual(self.qos.read_text().strip(), '123')

    def test_default_uses_kernel_setting(self):
        self.default.write_text('0\n')
        self.apply('default')
        self.assertEqual(self.qos.read_text().strip(), '0')

    def test_one_controller_failure_does_not_block_others(self):
        self.qos.unlink()
        second = self.add_controller('nvme1')
        with self.assertRaisesRegex(RuntimeError, 'nvme0'):
            self.apply('disabled')
        self.assertEqual(second.read_text().strip(), '0')

    def test_invalid_value_cannot_write(self):
        for value in ('echo 0', {}, [], True):
            with self.assertRaises(ValueError):
                self.apply(value)
        self.assertEqual(self.qos.read_text().strip(), '100000')

    def test_readback_failure_keeps_removal_marker(self):
        original = Path.read_text

        def unchanged(path, *args, **kwargs):
            if path == self.qos:
                return '100000\n'
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', unchanged):
            with self.assertRaisesRegex(RuntimeError, 'readback'):
                self.apply('disabled')
        self.assertTrue(list(self.managed.iterdir()))
        self.apply(None)
        self.assertEqual(self.qos.read_text().strip(), '100000')

    def test_failed_restore_is_retried(self):
        self.apply('disabled')
        self.default.write_text('invalid')
        with self.assertRaises(RuntimeError):
            self.apply(None)
        self.assertTrue(list(self.managed.iterdir()))
        self.default.write_text('25000')
        self.apply(None)
        self.assertEqual(self.qos.read_text().strip(), '25000')
        self.assertEqual(list(self.managed.iterdir()), [])


class UnsupportedApstTests(ApstTests):
    def absent_interface(self):
        self.qos.unlink()
        controller = self.qos.parent.parent
        (controller / 'state').write_text('live')
        return controller

    def test_confirmed_unsupported_controller_is_success_and_others_apply(self):
        self.absent_interface()
        second = self.add_controller('nvme1')
        with patch('reefy.policies.subprocess.run', return_value=Mock(
                returncode=0, stdout='{"apsta": 0}')) as identify:
            self.apply('disabled')
        self.assertEqual(second.read_text().strip(), '0')
        identify.assert_called_once_with(
            ['nvme', 'id-ctrl', '/dev/nvme0', '-o', 'json'],
            capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(len(list(self.managed.iterdir())), 1)

    def test_supported_controller_without_interface_still_warns(self):
        self.absent_interface()
        with patch('reefy.policies.subprocess.run', return_value=Mock(
                returncode=0, stdout='{"apsta": 1}')):
            with self.assertRaisesRegex(RuntimeError, 'no APST latency QoS interface'):
                self.apply('disabled')

    def test_capability_failure_is_not_silent_success(self):
        controller = self.absent_interface()
        for result in (Mock(returncode=1, stdout=''),
                       Mock(returncode=0, stdout='invalid'),
                       Mock(returncode=0, stdout='{}'),
                       Mock(returncode=0, stdout='{"apsta": "0"}')):
            with patch('reefy.policies.subprocess.run', return_value=result):
                with self.assertRaisesRegex(RuntimeError, 'cannot determine'):
                    self.apply('disabled')
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('nvme', 5)):
            with patch('reefy.policies.subprocess.run', side_effect=error):
                with self.assertRaisesRegex(RuntimeError, 'cannot determine'):
                    self.apply('disabled')
        (controller / 'state').write_text('resetting')
        with patch('reefy.policies.subprocess.run') as identify:
            with self.assertRaisesRegex(RuntimeError, 'not live'):
                self.apply('disabled')
            identify.assert_not_called()

    def test_existing_interface_uses_no_admin_reads(self):
        with patch('reefy.policies.subprocess.run') as identify:
            self.apply('disabled')
            self.apply(None)
            identify.assert_not_called()

    def test_unsupported_after_managed_apply_clears_marker(self):
        self.apply('disabled')
        self.absent_interface()
        with patch('reefy.policies.subprocess.run', return_value=Mock(
                returncode=0, stdout='{"apsta": 0}')):
            self.apply(None)
        self.assertEqual(list(self.managed.iterdir()), [])
