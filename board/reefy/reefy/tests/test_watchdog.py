"""Unit tests for infrastructure liveness checks and recovery."""

import configparser
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

import _bootstrap  # noqa: F401
from reefy import watchdog


PROXY_CHECK = {
    'service': 'reefy-proxy',
    'url': 'http://127.0.0.1:8080/',
    'kind': 'http-response',
}


class ProbeTests(unittest.TestCase):
    @mock.patch.object(watchdog.urllib.request, 'urlopen')
    def test_cloudflared_requires_ready_connection(self, urlopen):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"status":200,"readyConnections":4}')
        urlopen.return_value = response

        self.assertTrue(watchdog.probe(
            'http://127.0.0.1:20241/ready', 'cloudflared'))

        response.__enter__.return_value.read.return_value = (
            b'{"status":200,"readyConnections":0}')
        self.assertFalse(watchdog.probe(
            'http://127.0.0.1:20241/ready', 'cloudflared'))

    @mock.patch.object(watchdog.urllib.request, 'urlopen')
    def test_proxy_http_error_still_proves_liveness(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            PROXY_CHECK['url'], 403, 'Forbidden', {}, io.BytesIO())
        self.assertTrue(watchdog.probe(
            PROXY_CHECK['url'], PROXY_CHECK['kind']))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    @mock.patch.object(watchdog.subprocess, 'run')
    @mock.patch.object(watchdog, 'probe', return_value=False)
    @mock.patch.object(watchdog, 'find_container', return_value='container123456')
    def test_restarts_after_three_consecutive_failures(
            self, find_container, probe, run):
        run.return_value = types.SimpleNamespace(returncode=0)

        self.assertFalse(watchdog.check_service(
            PROXY_CHECK, self.tempdir.name, now=100))
        self.assertFalse(watchdog.check_service(
            PROXY_CHECK, self.tempdir.name, now=101))
        run.assert_not_called()

        self.assertFalse(watchdog.check_service(
            PROXY_CHECK, self.tempdir.name, now=102))
        run.assert_called_once_with(
            ['docker', 'restart', '--time', '5', 'container123456'],
            capture_output=True,
            text=True,
            timeout=watchdog.RESTART_TIMEOUT_SECONDS,
        )
        self.assertFalse(os.path.exists(os.path.join(
            self.tempdir.name, 'reefy-proxy-failures')))

    @mock.patch.object(watchdog.subprocess, 'run')
    @mock.patch.object(watchdog, 'probe', return_value=False)
    @mock.patch.object(watchdog, 'find_container', return_value='container123456')
    def test_cooldown_suppresses_restart(self, find_container, probe, run):
        with open(os.path.join(
                self.tempdir.name, 'reefy-proxy-failures'), 'w') as stream:
            stream.write('2')
        with open(os.path.join(
                self.tempdir.name, 'reefy-proxy-cooldown'), 'w') as stream:
            stream.write('500')

        self.assertFalse(watchdog.check_service(
            PROXY_CHECK, self.tempdir.name, now=200))
        run.assert_not_called()

    @mock.patch.object(watchdog, 'probe', return_value=True)
    @mock.patch.object(watchdog, 'find_container', return_value='container123456')
    def test_healthy_probe_clears_failure_count(self, find_container, probe):
        failure_file = os.path.join(
            self.tempdir.name, 'reefy-proxy-failures')
        with open(failure_file, 'w') as stream:
            stream.write('2')

        self.assertTrue(watchdog.check_service(
            PROXY_CHECK, self.tempdir.name, now=100))
        self.assertFalse(os.path.exists(failure_file))

    @mock.patch.object(watchdog, 'probe')
    @mock.patch.object(watchdog, 'find_container', return_value=None)
    def test_absent_optional_service_is_skipped(self, find_container, probe):
        self.assertTrue(watchdog.check_service(
            PROXY_CHECK, self.tempdir.name, now=100))
        probe.assert_not_called()


class ContainerDiscoveryTests(unittest.TestCase):
    @mock.patch.object(watchdog.subprocess, 'run')
    def test_prefers_apps_v2_system_project(self, run):
        run.return_value = types.SimpleNamespace(
            returncode=0, stdout='v2-container\n')

        self.assertEqual(
            watchdog.find_container('reefy-proxy'), 'v2-container')
        run.assert_called_once_with(
            [
                'docker', 'ps', '-aq',
                '--filter',
                'label=com.docker.compose.project=reefy-system',
                '--filter',
                'label=com.docker.compose.service=reefy-proxy',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

    @mock.patch.object(watchdog.subprocess, 'run')
    def test_falls_back_to_legacy_system_project(self, run):
        run.side_effect = [
            types.SimpleNamespace(returncode=0, stdout=''),
            types.SimpleNamespace(returncode=0, stdout='v1-container\n'),
        ]

        self.assertEqual(
            watchdog.find_container('cloudflared'), 'v1-container')
        self.assertEqual(
            run.call_args_list[1].args[0][4],
            'label=com.docker.compose.project=state')

    @mock.patch.object(watchdog.subprocess, 'run')
    def test_returns_none_when_service_is_absent(self, run):
        run.return_value = types.SimpleNamespace(returncode=0, stdout='')

        self.assertIsNone(watchdog.find_container('reefy-proxy'))
        self.assertEqual(run.call_count, len(watchdog.COMPOSE_PROJECTS))


class RestartCompletionTests(unittest.TestCase):
    def test_restart_ack_after_stop_grace_completes_and_preserves_data(self):
        # Exercise the actual subprocess deadline. Docker's stop grace does
        # not include all daemon cleanup, reaping and container startup work.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            state.mkdir()
            (state / 'reefy-proxy-failures').write_text('2')
            preserved = root / 'synthetic-persistent-data'
            preserved.write_bytes(b'preserve unrelated application data')
            docker = root / 'docker'
            docker.write_text(
                '#!' + sys.executable + '\n'
                'import sys,time\n'
                'assert sys.argv[1:3] == ["restart", "--time"]\n'
                'time.sleep(16)\n'
                'print("synthetic-container")\n')
            docker.chmod(0o700)
            output = io.StringIO()
            with mock.patch.dict(os.environ, {
                    'PATH': directory + os.pathsep + os.environ.get('PATH', '')}), \
                    mock.patch.object(watchdog, 'find_container',
                                      return_value='synthetic-container'), \
                    mock.patch.object(watchdog, 'probe', return_value=False), \
                    contextlib.redirect_stdout(output):
                self.assertFalse(watchdog.check_service(
                    PROXY_CHECK, str(state), now=100))
            self.assertIn('reefy-proxy: restart completed', output.getvalue())
            self.assertNotIn('restart failed', output.getvalue())
            self.assertFalse((state / 'reefy-proxy-failures').exists())
            self.assertEqual((state / 'reefy-proxy-cooldown').read_text(), '400')
            self.assertEqual(preserved.read_bytes(),
                             b'preserve unrelated application data')

    def test_failed_restart_does_not_claim_recovery_or_change_data(self):
        outcomes = (
            types.SimpleNamespace(returncode=1),
            OSError('synthetic execution failure'),
            subprocess.TimeoutExpired('synthetic-docker', 30),
        )
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__), \
                    tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'reefy-proxy-failures').write_text('2')
                preserved = root / 'synthetic-persistent-data'
                preserved.write_bytes(b'preserve unrelated application data')
                output = io.StringIO()
                options = ({'side_effect': outcome}
                           if isinstance(outcome, BaseException)
                           else {'return_value': outcome})
                with mock.patch.object(watchdog, 'find_container',
                                       return_value='synthetic-container'), \
                        mock.patch.object(watchdog, 'probe', return_value=False), \
                        mock.patch.object(watchdog.subprocess, 'run', **options), \
                        contextlib.redirect_stdout(output):
                    self.assertFalse(watchdog.check_service(
                        PROXY_CHECK, directory, now=100))
                self.assertIn('restart failed or timed out', output.getvalue())
                self.assertNotIn('restart completed', output.getvalue())
                self.assertEqual((root / 'reefy-proxy-failures').read_text(), '3')
                self.assertEqual((root / 'reefy-proxy-cooldown').read_text(), '400')
                self.assertEqual(preserved.read_bytes(),
                                 b'preserve unrelated application data')

    def test_oneshot_budget_contains_both_bounded_service_checks(self):
        service = Path(watchdog.__file__).parents[2] / 'systemd/system/reefy-watchdog.service'
        config = configparser.ConfigParser()
        config.read(service)
        budget = int(config['Service']['TimeoutStartSec'].removesuffix('s'))
        # Each existing project lookup has a five-second subprocess bound.
        per_service = (len(watchdog.COMPOSE_PROJECTS) * 5
                       + watchdog.PROBE_TIMEOUT_SECONDS
                       + watchdog.RESTART_TIMEOUT_SECONDS)
        self.assertGreater(budget, len(watchdog.CHECKS) * per_service)


if __name__ == '__main__':
    unittest.main()
