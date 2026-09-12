"""Persisted policy barrier, exact scope and runtime/init restoration."""
import subprocess
import unittest
from unittest.mock import Mock, patch, call

import _bootstrap  # noqa: F401
from reefy import app_restart


class RestartPolicyTests(unittest.TestCase):
    def test_stop_inventories_all_project_containers_and_disables_restart(self):
        ids = ['a' * 64, 'b' * 64]
        with patch.object(app_restart, '_docker', side_effect=['\n'.join(ids), '']) as docker:
            app_restart.set_policy('synthetic-project', 'no')
        self.assertEqual(docker.call_args_list, [
            call(['ps', '--all', '--quiet', '--no-trunc', '--filter',
                  'label=com.docker.compose.project=synthetic-project']),
            call(['update', '--restart=no', *ids]),
        ])

    def test_legacy_service_is_scoped_within_shared_project(self):
        with patch.object(app_restart, '_docker', return_value='') as docker:
            app_restart.set_policy('state', 'no', 'synthetic-app')
        self.assertIn('label=com.docker.compose.service=synthetic-app', docker.call_args.args[0])
        docker.assert_called_once()

    def test_no_containers_is_safe_and_never_issues_broad_update(self):
        with patch.object(app_restart, '_docker', return_value='') as docker:
            app_restart.set_policy('synthetic-project', 'no')
        docker.assert_called_once()

    def test_invalid_inventory_fails_closed(self):
        with patch.object(app_restart, '_docker', return_value='--all') as docker:
            with self.assertRaises(RuntimeError):
                app_restart.set_policy('synthetic-project', 'no')
        docker.assert_called_once()

    def test_empty_project_is_rejected(self):
        with patch.object(app_restart, '_docker') as docker:
            with self.assertRaises(ValueError):
                app_restart.set_policy('', 'no')
        docker.assert_not_called()

    def test_restore_respects_service_selection_and_retry_limit(self):
        config = {'services': {'app': {'restart': 'on-failure:3'},
                               'setup': {'restart': 'no'},
                               'optional': {'restart': 'on-failure'}}}
        with patch.object(app_restart, 'set_policy') as policy:
            app_restart.restore_policies('synthetic-project', config, ['app'])
        policy.assert_called_once_with('synthetic-project', 'on-failure:3', 'app')
        with patch.object(app_restart, 'set_policy') as policy:
            app_restart.restore_policies('synthetic-project', config, ['setup'])
        policy.assert_called_once_with('synthetic-project', 'no', 'setup')

    def test_docker_errors_and_timeouts_are_not_swallowed(self):
        with patch.object(app_restart.subprocess, 'run',
                          return_value=Mock(returncode=1, stderr='synthetic failure', stdout='')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                app_restart.set_policy('synthetic-project', 'no')
        with patch.object(app_restart.subprocess, 'run', side_effect=subprocess.TimeoutExpired('docker', 30)):
            with self.assertRaisesRegex(RuntimeError, 'TimeoutExpired'):
                app_restart.set_policy('synthetic-project', 'no')
