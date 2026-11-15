"""Regression tests for reefy-terminal-bridge subscriber accounting."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path


BRIDGE_PATH = (
    Path(__file__).resolve().parents[1]
    / 'rootfs-overlay/usr/bin/reefy-terminal-bridge'
)


def _load_bridge():
    fake_paho = types.ModuleType('paho')
    fake_paho_mqtt = types.ModuleType('paho.mqtt')
    fake_client = types.ModuleType('paho.mqtt.client')
    fake_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=object())
    fake_client.Client = object
    sys.modules.setdefault('paho', fake_paho)
    sys.modules.setdefault('paho.mqtt', fake_paho_mqtt)
    sys.modules.setdefault('paho.mqtt.client', fake_client)

    loader = importlib.machinery.SourceFileLoader(
        'reefy_terminal_bridge_for_test', str(BRIDGE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


bridge = _load_bridge()


class TerminalBridgeSubscriberTests(unittest.TestCase):
    def setUp(self):
        bridge._sessions.clear()

    def test_client_id_subscribe_is_idempotent(self):
        self.assertEqual(
            bridge.subscribe_session('app1', {'client_id': 'tab-a'}, now=10),
            1)
        self.assertEqual(
            bridge.subscribe_session('app1', {'client_id': 'tab-a'}, now=20),
            1)
        session = bridge.get_session('app1')
        self.assertEqual(session['subscribers'], {'tab-a': True})
        self.assertEqual(session['last_activity'], 20)

    def test_mqtt_client_id_is_unique_per_device(self):
        first = bridge.mqtt_client_id(
            '11111111-1111-4111-8111-111111111111')
        second = bridge.mqtt_client_id(
            '22222222-2222-4222-8222-222222222222')

        self.assertEqual(
            first, 'reefy-terminal-11111111-1111-4111-8111-111111111111')
        self.assertNotEqual(first, second)

    def test_multiple_client_ids_count_as_multiple_viewers(self):
        self.assertEqual(
            bridge.subscribe_session('app1', {'client_id': 'tab-a'}, now=10),
            1)
        self.assertEqual(
            bridge.subscribe_session('app1', {'client_id': 'tab-b'}, now=11),
            2)
        self.assertEqual(
            bridge.unsubscribe_session('app1', {'client_id': 'tab-a'}),
            1)
        self.assertEqual(
            bridge.unsubscribe_session('app1', {'client_id': 'tab-b'}),
            0)

    def test_unknown_client_id_unsubscribe_is_idempotent(self):
        self.assertEqual(
            bridge.subscribe_session('app1', {'client_id': 'tab-a'}, now=10),
            1)
        self.assertEqual(
            bridge.unsubscribe_session('app1', {'client_id': 'missing'}),
            1)

    def test_legacy_messages_keep_counter_fallback(self):
        self.assertEqual(bridge.subscribe_session('app1', {}, now=10), 1)
        self.assertEqual(bridge.subscribe_session('app1', {}, now=11), 2)
        self.assertEqual(bridge.unsubscribe_session('app1', {}), 1)
        self.assertEqual(bridge.unsubscribe_session('app1', {}), 0)
        self.assertEqual(bridge.unsubscribe_session('app1', {}), 0)

    def test_subscribe_records_container_metadata(self):
        self.assertEqual(
            bridge.subscribe_session(
                'app1',
                {'client_id': 'tab-a', 'container': 'state-app1-1', 'name': 'Dev'},
                now=10),
            1)
        session = bridge.get_session('app1')
        self.assertEqual(session['container'], 'state-app1-1')
        self.assertEqual(session['name'], 'Dev')

    def test_v2_resolution_uses_live_container_resolver(self):
        state = {'schema_version': 2, 'apps': []}
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir) / 'desired-state-v2.json'
            state_path.write_text(json.dumps(state))
            with mock.patch.object(bridge, 'STATE_DIR', state_dir), \
                    mock.patch.object(
                        bridge, 'resolve_container',
                        return_value='state-app1-1') as resolver:
                self.assertEqual(
                    bridge._resolve_app_container(
                        'app1', 'reefy-app-app1-app-1'),
                    'state-app1-1')

        resolver.assert_called_once_with(
            state, 'app1', 'reefy-app-app1-app-1')





class TerminalCredentialTransitionTests(unittest.TestCase):
    def test_adoption_rebuilds_mqtt_only_and_keeps_live_pty_until_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('device.key', 'bootstrap.crt', 'bootstrap.key', 'ca.crt'):
                (root / name).write_text('synthetic-' + name)
            (root / 'device-uuid').write_text('11111111-1111-4111-8111-111111111111')
            (root / 'bootstrap-uuid').write_text('11111111-1111-4111-8111-111111111111')
            sentinel = root / 'app-data'
            sentinel.write_bytes(b'synthetic-data-preserved')
            config = {'MQTT_BROKER': 'synthetic.invalid', 'MQTT_USER_ID': 'synthetic',
                      'MQTT_CA_CERT': str(root / 'ca.crt'),
                      'MQTT_CLIENT_CERT': str(root / 'bootstrap.crt'),
                      'MQTT_CLIENT_KEY': str(root / 'bootstrap.key')}
            old, new = mock.MagicMock(), mock.MagicMock()
            observed = []
            def first_connected():
                (root / 'device.crt').write_text('synthetic-adopted-certificate')
            def replacement_connected():
                observed.append('host' in bridge._sessions)
                observed.append(sentinel.read_bytes() == b'synthetic-data-preserved')
                raise KeyboardInterrupt()
            old.loop_start.side_effect = first_connected
            new.loop_start.side_effect = replacement_connected
            bridge._sessions['host'] = {'synthetic': 'live-pty'}
            with mock.patch.object(bridge, 'STATE_DIR', str(root)), \
                    mock.patch.object(bridge,'BOOTSTRAP_UUID_PATH',str(root/'bootstrap-uuid')), \
                    mock.patch.object(bridge, 'load_config', return_value=config), \
                    mock.patch.object(bridge.mqtt, 'Client', side_effect=[old, new]), \
                    mock.patch.object(bridge.threading, 'Thread'), \
                    mock.patch.object(bridge, 'kill_session') as kill:
                bridge.main()
            self.assertEqual(observed, [True, True])
            old.tls_set.assert_called_once()
            new.tls_set.assert_called_once()
            self.assertEqual(old.tls_set.call_args.kwargs['certfile'], str(root / 'bootstrap.crt'))
            self.assertEqual(new.tls_set.call_args.kwargs['certfile'], str(root / 'device.crt'))
            old.disconnect.assert_called_once()
            new.connect_async.assert_called_once()
            self.assertEqual(kill.call_count, 1)  # final shutdown only
            self.assertEqual((root / 'device.key').read_text(), 'synthetic-device.key')
            bridge._sessions.clear()

    def test_denied_suback_is_not_connection_readiness(self):
        self.assertTrue(bridge.subscription_accepted([0, 1]))
        self.assertFalse(bridge.subscription_accepted([128]))
        self.assertFalse(bridge.subscription_accepted([mock.Mock(value=135)]))
        self.assertFalse(bridge.subscription_accepted([]))


class TerminalProofConnectionTests(unittest.TestCase):
    def test_every_bootstrap_connect_gets_fresh_proof_and_exact_host_topics(self):
        from reefy import bootstrap_terminal
        identity={'uuid':'11111111-1111-4111-8111-111111111111','bootstrap':True,
                  'config':{'MQTT_USER_ID':'synthetic-owner'},
                  'ca':'synthetic-ca','certificate':'synthetic-certificate','key':'synthetic-key'}
        client=mock.MagicMock()
        with mock.patch.object(bridge.mqtt,'Client',return_value=client), \
                mock.patch.object(bootstrap_terminal,'connection_proof',side_effect=['proof-one','proof-two']):
            bridge.configured_client(identity)
            client.on_pre_connect(client,None)
            client.on_pre_connect(client,None)
        self.assertEqual(client.username_pw_set.call_args_list,
            [mock.call('synthetic-owner','proof-one'),mock.call('synthetic-owner','proof-two')])
        self.assertEqual(bridge.terminal_subscriptions(identity,'synthetic/base'),
            [('synthetic/base/host/input',0),('synthetic/base/host/control',1)])





class TerminalBootstrapIdentityTests(unittest.TestCase):
    def test_preserved_disk_uuid_cannot_override_this_boot_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            current='11111111-1111-4111-8111-111111111111'
            old='22222222-2222-4222-8222-222222222222'
            (root/'device-uuid').write_text(old)
            (root/'bootstrap-uuid').write_text(current)
            for name in ('bootstrap.crt','bootstrap.key','device.key','ca.crt'):
                (root/name).write_text('synthetic-'+name)
            config={'MQTT_CLIENT_CERT':str(root/'bootstrap.crt'),
                    'MQTT_CLIENT_KEY':str(root/'bootstrap.key'),'MQTT_CA_CERT':str(root/'ca.crt')}
            with mock.patch.object(bridge,'STATE_DIR',str(root)), \
                    mock.patch.object(bridge,'BOOTSTRAP_UUID_PATH',str(root/'bootstrap-uuid')), \
                    mock.patch.object(bridge,'load_config',return_value=config):
                self.assertEqual(bridge.connection_identity()['uuid'],current)
                (root/'device.crt').write_text('synthetic-adopted')
                self.assertEqual(bridge.connection_identity()['uuid'],old)


if __name__ == '__main__':
    unittest.main()
