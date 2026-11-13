"""Boot reconciliation tolerates a late socket without extending other calls."""
from contextlib import nullcontext
import sys
import types
import unittest
from unittest import mock

from test_control import _load_control_module


class ControlStartupWaitTests(unittest.TestCase):
    def exercise(self, available_after, *, method=None, invalid_reply=False):
        control = _load_control_module()
        plane = control.ControlPlane()
        plane.mode = 'device'
        elapsed = [0.0]
        calls = []

        def sleep(seconds):
            elapsed[0] += seconds

        class Connection:
            def open(self, interface):
                self_test.assertEqual(interface, 'io.reefy.Reconciler')
                return nullcontext(self)

            def SubmitReconcile(self, **kwargs):
                # The startup wait is a local client option, never a wire field.
                self_test.assertEqual(kwargs, {})
                calls.append('SubmitReconcile')
                return {'ok': True, 'request_id': 'synthetic-startup'}

            def WaitApply(self, request_id):
                self_test.assertEqual(request_id, 'synthetic-startup')
                calls.append('WaitApply')
                return {'found': True, 'request_id': request_id,
                        'status': 'succeeded', 'applied': True,
                        'warnings': [], 'error': ''}

        self_test = self

        def connect(address):
            self.assertEqual(address, plane.VARLINK_ADDRESS)
            if invalid_reply:
                raise ValueError('synthetic invalid reply')
            if available_after is None or elapsed[0] < available_after:
                raise FileNotFoundError('synthetic socket not present')
            return nullcontext(Connection())

        varlink = types.ModuleType('varlink')
        varlink.Client = types.SimpleNamespace(new_with_address=connect)
        with mock.patch.dict(sys.modules, {'varlink': varlink}), \
                mock.patch.object(control.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                mock.patch.object(control.time, 'sleep', side_effect=sleep), \
                mock.patch.object(control, 'log'), \
                mock.patch.object(plane, '_publish_status') as status, \
                mock.patch.object(plane, '_publish_stage') as stage, \
                mock.patch.object(plane, '_publish_state_hash') as state_hash, \
                mock.patch.object(control.shared, 'wait_for_tunnel_health'):
            result = (plane._varlink_call(method) if method else
                      plane._handle_device_connect(mock.sentinel.client))
        return elapsed[0], calls, stage, status, state_hash, result

    def test_already_listening_socket_reconciles_immediately(self):
        elapsed, calls, stage, _, state_hash, _ = self.exercise(0)
        self.assertEqual(elapsed, 0)
        self.assertEqual(calls, ['SubmitReconcile', 'WaitApply'])
        stage.assert_called_once_with('ready', 'Device ready')
        state_hash.assert_called_once_with()

    def test_late_socket_still_reports_successful_saved_state(self):
        elapsed, calls, stage, _, state_hash, _ = self.exercise(80)
        self.assertEqual(elapsed, 80)
        self.assertEqual(calls, ['SubmitReconcile', 'WaitApply'])
        stage.assert_called_once_with('ready', 'Device ready')
        state_hash.assert_called_once_with()

    def test_absent_socket_remains_bounded_and_never_reports_ready(self):
        elapsed, calls, stage, status, state_hash, _ = self.exercise(None)
        self.assertLessEqual(elapsed, 120)
        self.assertEqual(calls, [])
        self.assertEqual(stage.call_args.args[0], 'error')
        status.assert_called_once_with('online', 'Device connected')
        state_hash.assert_not_called()

    def test_ordinary_calls_keep_the_existing_startup_budget(self):
        elapsed, _, _, _, _, result = self.exercise(None, method='GetStatus')
        self.assertLessEqual(elapsed, 30)
        self.assertFalse(result['ok'])

    def test_invalid_reply_keeps_the_existing_short_retry_budget(self):
        elapsed, calls, stage, _, state_hash, _ = self.exercise(0, invalid_reply=True)
        self.assertLessEqual(elapsed, 4)
        self.assertEqual(calls, [])
        self.assertEqual(stage.call_args.args[0], 'error')
        state_hash.assert_not_called()
