"""Radio ownership is serialized without holding the desired-state worker."""
import threading
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
from reefy import dataplane
from reefy.wifi_apply import WifiApply


class WifiApplyTests(unittest.TestCase):
    def test_latest_request_wins_and_removal_diffs_against_running_radio(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        first = {'ssid': 'synthetic-first', 'password': 'synthetic-secret'}
        skipped = {'ssid': 'synthetic-skipped'}

        def apply(wifi, old):
            calls.append((wifi, old))
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(5))

        worker = WifiApply(apply)
        worker.submit(first, None)
        try:
            self.assertTrue(entered.wait(2))
            worker.submit(skipped, first)
            worker.submit(None, skipped)
            self.assertTrue(worker.busy())
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            worker.wait()
        self.assertEqual(calls, [(first, None), (None, first)])

    def test_duplicate_inflight_target_is_coalesced_and_input_is_copied(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def apply(wifi, old):
            entered.set()
            release.wait(5)
            calls.append(wifi)
        worker = WifiApply(apply)
        target = {'ssid': 'synthetic-radio'}
        worker.submit(target, None)
        try:
            self.assertTrue(entered.wait(2))
            worker.submit(target, None)
            target['ssid'] = 'mutated-caller-input'
        finally:
            release.set()
            worker.wait()
        self.assertEqual(calls, [{'ssid': 'synthetic-radio'}])

    def test_exception_does_not_strand_pending_radio_change(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def apply(wifi, old):
            calls.append(wifi)
            if len(calls) == 1:
                entered.set()
                release.wait(5)
                raise RuntimeError('synthetic association failure')
        worker = WifiApply(apply)
        with mock.patch('reefy.wifi_apply.log') as log:
            worker.submit({'ssid': 'synthetic-first'}, None)
            try:
                self.assertTrue(entered.wait(2))
                worker.submit({'ssid': 'synthetic-next'}, None)
            finally:
                release.set()
                worker.wait()
            self.assertEqual(len(calls), 2)
            self.assertNotIn('association failure', str(log.call_args))
            self.assertFalse(worker.busy())

    def test_failed_network_operation_waits_for_pending_sole_uplink(self):
        dp = object.__new__(dataplane.DataPlane)
        dp._wifi_apply = mock.Mock()
        dp._wifi_apply.busy.return_value = True
        with mock.patch.object(dataplane.subprocess, 'run', return_value=mock.Mock(stdout='[]')):
            self.assertTrue(dp._wait_for_wifi_uplink())
        dp._wifi_apply.wait.assert_called_once_with()

    def test_existing_uplink_does_not_wait_and_absent_worker_is_safe(self):
        dp = object.__new__(dataplane.DataPlane)
        self.assertFalse(dp._wait_for_wifi_uplink())
        dp._wifi_apply = mock.Mock()
        dp._wifi_apply.busy.return_value = True
        with mock.patch.object(dataplane.subprocess, 'run', return_value=mock.Mock(stdout='[{"dev":"synthetic-ethernet"}]')), \
                mock.patch('builtins.open', mock.mock_open(read_data='up\n')):
            self.assertFalse(dp._wait_for_wifi_uplink())
        dp._wifi_apply.wait.assert_not_called()

    def test_local_artifact_success_never_waits_and_network_failure_retries_once(self):
        dp = object.__new__(dataplane.DataPlane)
        dp._preparation_local = threading.local()
        artifact = {'ref': 'synthetic@sha256:'+('a'*64)}
        success = mock.Mock(returncode=0, stdout='', stderr='')
        failure = mock.Mock(returncode=1, stdout='', stderr='registry request failed\n')
        with mock.patch.object(dataplane.subprocess, 'run', return_value=success), \
                mock.patch.object(dp, '_wait_for_wifi_uplink') as wait:
            self.assertTrue(dp._prepare_one_app_artifact({}, artifact))
            wait.assert_not_called()
        with mock.patch.object(dataplane.subprocess, 'run', side_effect=[failure, success]) as prepare, \
                mock.patch.object(dp, '_wait_for_wifi_uplink', return_value=True):
            self.assertTrue(dp._prepare_one_app_artifact({}, artifact))
            self.assertEqual(prepare.call_count, 2)
        with mock.patch.object(dataplane.subprocess, 'run', return_value=failure) as prepare, \
                mock.patch.object(dp, '_wait_for_wifi_uplink', return_value=True) as wait:
            self.assertFalse(dp._prepare_one_app_artifact({}, artifact))
            self.assertEqual(prepare.call_count, 2)
            wait.assert_called_once_with()

    def test_local_artifact_refusals_never_wait_for_wifi_or_retry_activation(self):
        dp = object.__new__(dataplane.DataPlane)
        dp._preparation_local = threading.local()
        artifact = {'ref': 'synthetic@sha256:'+('a'*64)}
        for reason in ('blob digest mismatch', 'Reefy build identity mismatch',
                       'provider activation hook failed with exit 1',
                       'registry request failed with HTTP 404'):
            with self.subTest(reason=reason), \
                    mock.patch.object(dataplane.subprocess, 'run', return_value=mock.Mock(
                        returncode=1, stdout='', stderr=reason+'\n')) as prepare, \
                    mock.patch.object(dp, '_wait_for_wifi_uplink') as wait:
                self.assertFalse(dp._prepare_one_app_artifact({}, artifact))
                wait.assert_not_called()
                self.assertEqual(prepare.call_count, 1)

    def test_single_attempt_optional_image_pull_can_recover_after_wifi(self):
        dp = object.__new__(dataplane.DataPlane)
        dp._app_reconcile_local = threading.local()
        with mock.patch.object(dp, '_run_compose_command_streaming', side_effect=[
                (False, 'network is unreachable'), (True, '')]) as pull, \
                mock.patch.object(dp, '_wait_for_wifi_uplink', return_value=True) as wait:
            ok, output, reason = dp._pull_project_images(
                '/synthetic/compose.json', 'synthetic-project', ['optional'],
                dataplane.time.monotonic()+30, max_retries=1)
        self.assertTrue(ok, reason)
        self.assertEqual(pull.call_count, 2)
        wait.assert_called_once_with()

    def test_wifi_recovery_is_bounded_even_when_network_still_fails(self):
        dp = object.__new__(dataplane.DataPlane)
        dp._app_reconcile_local = threading.local()
        with mock.patch.object(dp, '_run_compose_command_streaming', return_value=(
                False, 'network is unreachable')) as pull, \
                mock.patch.object(dp, '_wait_for_wifi_uplink', return_value=True) as wait:
            ok, output, reason = dp._pull_project_images(
                '/synthetic/compose.json', 'synthetic-project', ['optional'],
                dataplane.time.monotonic()+30, max_retries=1)
        self.assertFalse(ok)
        self.assertEqual(pull.call_count, 2)
        wait.assert_called_once_with()

    def test_invalid_compose_does_not_wait_for_wifi_or_gain_an_extra_pull(self):
        dp = object.__new__(dataplane.DataPlane)
        dp._app_reconcile_local = threading.local()
        with mock.patch.object(dp, '_run_compose_command_streaming', return_value=(
                False, 'invalid compose configuration')) as pull, \
                mock.patch.object(dp, '_wait_for_wifi_uplink') as wait:
            ok, output, reason = dp._pull_project_images(
                '/synthetic/compose.json', 'synthetic-project', ['optional'],
                dataplane.time.monotonic()+30, max_retries=1)
        self.assertFalse(ok)
        self.assertEqual(pull.call_count, 1)
        wait.assert_not_called()
