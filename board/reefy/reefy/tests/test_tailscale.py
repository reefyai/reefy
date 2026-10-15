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
        raw = {'BackendState': 'NeedsLogin'}
        temporary_file = tempfile.NamedTemporaryFile

        def run(argv, **kwargs):
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
            self.assertEqual(command.call_count, 1)
        self.assertFalse(list(self.root.glob('secret-*')))
        self.assertNotIn(self.payload['auth_key'], tailscale.BINDING.read_text())
        self.assertEqual(tailscale.BINDING.stat().st_mode & 0o777, 0o600)
        rebooted = tailscale.Tailscale(self.publish)
        with patch.object(rebooted, '_raw_status', return_value=raw):
            self.assertEqual(rebooted.status()['state'], 'connected')

    def test_foreign_binding_never_reauthenticates(self):
        tailscale.BINDING.write_text(json.dumps({'connection_id': 'other-connection'}))
        with patch.object(self.manager, '_raw_status', return_value={'BackendState': 'NeedsLogin'}):
            self.manager.enroll(self.payload, self.config)
        self.assertEqual(self.manager.error, 'connection_changed')
        self.assertEqual(json.loads(tailscale.BINDING.read_text())['connection_id'], 'other-connection')


if __name__ == '__main__':
    unittest.main()
