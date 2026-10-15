"""Tailscale persistence, secret handling, and enrollment replay coverage."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import _bootstrap
from reefy import tailscale


class TailscaleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.addCleanup(patch.stopall)
        patch.object(tailscale, 'STATE_DIR', self.root).start()
        patch.object(tailscale, 'BINDING', self.root / 'reefy-connection.json').start()
        patch.object(tailscale, 'durable_storage', return_value=True).start()
        self.publish = Mock()
        self.manager = tailscale.Tailscale(self.publish)
        self.config = {'enabled': True, 'connection_id': 'synthetic-connection'}
        self.payload = {**self.config, 'request_id': self.manager.request_id,
                        'auth_key': 'tskey-auth-synthetic-enrollment'}

    def test_daemon_uses_persistent_state(self):
        root = Path(os.environ.get('REEFY_TAILSCALE_FIRMWARE_ROOT',
                                    Path(__file__).resolve().parents[4]))
        service = root / 'board/reefy/reefy/rootfs-overlay/usr/lib/systemd/system/tailscaled.service'
        source = service.read_text()
        self.assertIn('--statedir=/mnt/reefy-data/state/tailscale', source)
        self.assertIn('After=reefy-storage.service', source)
        self.assertIn('ExecStartPre=/usr/bin/reefy-tailscale-state', source)

    def test_ram_storage_cannot_enroll(self):
        with patch.object(tailscale, 'durable_storage', return_value=False):
            self.manager.tick(self.config)
            self.assertEqual(self.manager.status()['state'], 'storage_unavailable')
        self.publish.assert_not_called()

    def test_removed_config_rejects_inflight_key(self):
        self.manager.enroll(self.payload, {'enabled': False})
        self.assertFalse(tailscale.BINDING.exists())
        self.publish.assert_not_called()

    def test_stale_reply_rejected(self):
        self.manager.enroll(dict(self.payload, request_id='old-request'), self.config)
        self.assertFalse(tailscale.BINDING.exists())

    def test_existing_connection_preserved(self):
        with patch.object(self.manager, '_raw_status', return_value={
                'BackendState': 'Running', 'Self': {'ID': 'synthetic-node'}}):
            self.manager.tick(self.config)
            self.manager.enroll(self.payload, self.config)
        self.assertFalse(tailscale.BINDING.exists())
        self.assertEqual(self.publish.call_args.args[1]['state'], 'unmanaged')

    def test_enrollment_uses_temporary_secret_and_replays_without_login(self):
        self.config['hostname'] = 'synthetic-node'
        raw = {'BackendState': 'NeedsLogin'}
        temporary_file = tempfile.NamedTemporaryFile

        def run(argv, **kwargs):
            if argv[:2] == ['tailscale', 'set']:
                self.assertEqual(argv, ['tailscale', 'set', '--hostname=synthetic-node'])
                return Mock(returncode=0)
            self.assertIn('--hostname=', argv)
            auth = next(arg for arg in argv if arg.startswith('--auth-key=file:'))
            path = Path(auth.split('file:', 1)[1])
            self.assertEqual(path.read_text(), self.payload['auth_key'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(self.payload['auth_key'], ' '.join(argv))
            self.assertTrue(tailscale.BINDING.exists())
            raw.update(BackendState='Running', Self={'ID': 'synthetic-node'})
            return Mock(returncode=0)

        with patch.object(self.manager, '_raw_status', side_effect=lambda: raw):
            with patch.object(tailscale.subprocess, 'run', side_effect=run) as command:
                with patch.object(tailscale.tempfile, 'NamedTemporaryFile',
                                  side_effect=lambda **kwargs: temporary_file(
                                      mode='w', dir=self.root, prefix='secret-')):
                    self.manager.enroll(self.payload, self.config)
                self.manager.enroll(self.payload, self.config)
            self.assertEqual(command.call_count, 2)
        self.assertFalse(list(self.root.glob('secret-*')))
        self.assertNotIn(self.payload['auth_key'], tailscale.BINDING.read_text())
        self.assertEqual(tailscale.BINDING.stat().st_mode & 0o777, 0o600)
        rebooted = tailscale.Tailscale(self.publish)
        with patch.object(rebooted, '_raw_status', return_value=raw):
            self.assertEqual(rebooted.status()['state'], 'connected')

    def test_provider_rejected_hostname_does_not_block_enrollment(self):
        for name in ('a' * 64, 'synthetic-caf\u00e9'):
            with self.subTest(name=name):
                tailscale.BINDING.unlink(missing_ok=True)
                manager = tailscale.Tailscale(self.publish)
                config = dict(self.config, hostname=name)
                payload = dict(self.payload, request_id=manager.request_id)
                raw = {'BackendState': 'NeedsLogin'}
                temporary_file = tempfile.NamedTemporaryFile

                def run(argv, **kwargs):
                    if argv[:2] == ['tailscale', 'up']:
                        # These names fail the up CLI's explicit DNS-label
                        # check. Login must not depend on that validation.
                        self.assertIn('--hostname=', argv)
                        self.assertNotIn('--hostname=' + name, argv)
                        raw.update(BackendState='Running', Self={'ID': 'synthetic-node'})
                        return Mock(returncode=0)
                    self.assertEqual(argv, ['tailscale', 'set', '--hostname=' + name])
                    return Mock(returncode=1)

                with patch.object(manager, '_raw_status', side_effect=lambda: raw), \
                        patch.object(tailscale.subprocess, 'run', side_effect=run) as command, \
                        patch.object(tailscale.tempfile, 'NamedTemporaryFile',
                                     side_effect=lambda **kwargs: temporary_file(
                                         mode='w', dir=self.root, prefix='secret-')):
                    manager.enroll(payload, config)
                    self.assertEqual(manager.status()['state'], 'connected')
                    self.assertIsNone(manager.error)
                    binding = tailscale.BINDING.read_bytes()
                    manager.tick(config)
                    self.assertEqual(sum(call.args[0][1] == 'up' for call in command.call_args_list), 1)
                    self.assertEqual(tailscale.BINDING.read_bytes(), binding)
                    self.assertIsNone(manager._applied_hostname)

    def test_foreign_binding_never_reauthenticates(self):
        tailscale.BINDING.write_text(json.dumps({'connection_id': 'other-connection'}))
        with patch.object(self.manager, '_raw_status', return_value={'BackendState': 'NeedsLogin'}):
            self.manager.enroll(self.payload, self.config)
        self.assertEqual(self.manager.error, 'connection_changed')
        self.assertEqual(json.loads(tailscale.BINDING.read_text())['connection_id'], 'other-connection')


class HostnamePersistenceTests(unittest.TestCase):
    def setUp(self):
        TailscaleTests.setUp(self)
        self.config['hostname'] = 'synthetic-node'
        tailscale.BINDING.write_text(json.dumps({'connection_id': self.config['connection_id']}))
        self.raw = {'BackendState': 'Running', 'Self': {
            'ID': 'synthetic-node-id', 'HostName': 'reefy-001122334455'}}
        patch.object(self.manager, '_raw_status', return_value=self.raw).start()

    def test_reboot_captured_bootstrap_name_is_corrected_without_reenrollment(self):
        binding = tailscale.BINDING.read_bytes()
        with patch.object(tailscale.subprocess, 'run', return_value=Mock(returncode=0)) as command:
            self.manager.tick(self.config)
            command.assert_called_once_with(
                ['tailscale', 'set', '--hostname=synthetic-node'],
                capture_output=True, timeout=10)
            self.manager.tick(self.config)
            self.assertEqual(command.call_count, 1)
        self.assertEqual(tailscale.BINDING.read_bytes(), binding)
        self.publish.assert_not_called()
        self.assertEqual(self.manager.status()['state'], 'connected')

    def test_device_rename_updates_preference(self):
        with patch.object(tailscale.subprocess, 'run', return_value=Mock(returncode=0)) as command:
            self.manager.tick(self.config)
            self.manager.tick(dict(self.config, hostname='synthetic-renamed'))
            self.assertEqual(command.call_args.args[0],
                             ['tailscale', 'set', '--hostname=synthetic-renamed'])
            self.assertEqual(command.call_count, 2)

    def test_failed_preference_update_retries_without_login_or_state_loss(self):
        binding = tailscale.BINDING.read_bytes()
        with patch.object(tailscale.subprocess, 'run', side_effect=[Mock(returncode=1), Mock(returncode=0)]) as command:
            self.manager.tick(self.config)
            self.manager.tick(self.config)
            self.assertEqual(command.call_count, 2)
            self.assertTrue(all(call.args[0][1] == 'set' for call in command.call_args_list))
        self.assertEqual(tailscale.BINDING.read_bytes(), binding)

    def test_unmanaged_or_foreign_connection_is_not_renamed(self):
        for binding in ({}, {'connection_id': 'foreign-connection'}):
            tailscale.BINDING.write_text(json.dumps(binding))
            with patch.object(tailscale.subprocess, 'run') as command:
                self.manager.tick(self.config)
                command.assert_not_called()

    def test_disabled_connection_is_not_renamed(self):
        with patch.object(tailscale.subprocess, 'run') as command:
            self.manager.tick(dict(self.config, enabled=False))
            command.assert_not_called()


    def test_stopped_node_keeps_its_pause_while_name_is_updated(self):
        self.raw['BackendState'] = 'Stopped'
        with patch.object(tailscale.subprocess, 'run', return_value=Mock(returncode=0)) as command:
            self.manager.tick(self.config)
            self.assertEqual(command.call_args.args[0],
                             ['tailscale', 'set', '--hostname=synthetic-node'])
        self.assertEqual(self.manager.status()['state'], 'stopped')
        self.publish.assert_not_called()

    def test_rename_back_uses_latest_saved_name(self):
        with patch.object(tailscale.subprocess, 'run', return_value=Mock(returncode=0)) as command:
            self.manager.tick(self.config)
            self.manager.tick(dict(self.config, hostname='synthetic-renamed'))
            self.manager.tick(self.config)
            self.assertEqual(command.call_count, 3)
            self.assertEqual(command.call_args.args[0],
                             ['tailscale', 'set', '--hostname=synthetic-node'])


if __name__ == '__main__':
    unittest.main()
