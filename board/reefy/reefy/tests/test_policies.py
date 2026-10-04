"""Host policies use synthetic sysfs fixtures, never the host's real devices."""
import functools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401
from reefy.policies import apply_apst, apply_policies
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
        self.assertEqual(warnings[-1]['policy'], 'host.policies.bad')
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
        warning = {'code': 'policy.apply_failed', 'policy': 'host.policies.hardware.nvme.apst',
                   'error': 'cannot write /sys/example', 'extra': 'discard'}
        warnings = ApplyResultStore._sanitize_warnings([warning])
        self.assertEqual(warnings[0]['error'], 'cannot write [PATH]')
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
