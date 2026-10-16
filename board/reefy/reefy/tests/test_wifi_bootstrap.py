"""Synthetic lifecycle and secret-handling tests for the setup service."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import _bootstrap
from reefy import wifi_bootstrap as wifi


class BootstrapTests(unittest.TestCase):
    def test_networkd_can_read_configuration_under_private_service_mask(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'network' / 'setup.network'
            setup = object.__new__(wifi.Setup)
            setup.interface = 'wlan-test'
            previous = os.umask(0o077)
            try:
                with mock.patch.object(wifi, 'NETWORK', path), mock.patch.object(wifi, 'command'):
                    for ap in (True, False):
                        setup.configure_network(ap)
                        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o755)
                        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
                        self.assertNotIn('password', path.read_text())
            finally:
                os.umask(previous)

    def test_validates_inputs_without_echoing_secrets(self):
        good = {'ssid': 'Synthetic "网络"', 'password': 'synthetic-password'}
        self.assertEqual(wifi.validate_credentials(good), good)
        for value in ([], {}, {'ssid': 'x' * 33}, {'ssid': 'x\ny'},
                      {'ssid': 'sample', 'password': 'bad'},
                      {'ssid': 'sample', 'password': 'secret\nvalue'}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    wifi.validate_credentials(value)
                self.assertNotIn('secret\nvalue', str(raised.exception))

    def test_private_state_is_ram_only_and_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'credentials.json'
            candidate = {'ssid': 'synthetic', 'password': 'synthetic-password'}
            with mock.patch.object(wifi, 'CREDENTIALS', path):
                wifi.private_write(path, json.dumps(candidate))
                self.assertEqual(wifi.read_credentials(), candidate)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                path.write_text('bad private value')
                self.assertIsNone(wifi.read_credentials())
        self.assertTrue(str(wifi.CREDENTIALS).startswith('/run/'))

    def test_scan_deduplicates_by_signal_and_preserves_names(self):
        result = wifi.parse_scan('BSS aa(on wlan0)\n\tsignal: -70.0 dBm\n\tSSID: sample\n'
                                '\tRSN:\nBSS bb(on wlan0)\n\tsignal: -30.0 dBm\n\tSSID: sample\n'
                                '\tRSN:\nBSS cc(on wlan0)\n\tsignal: -50.0 dBm\n\tSSID: <synthetic>\n')
        self.assertEqual([n['ssid'] for n in result], ['sample', '<synthetic>'])
        self.assertEqual(result[0]['signal'], -30)
        self.assertEqual(result[0]['security'], 'secured')

    def test_scan_decodes_iw_escaped_unicode_and_backslashes(self):
        output = 'BSS aa(on wlan0)\n\tsignal: -40.0 dBm\n\tSSID: ' + r'\xe7\xbd\x91\xe7\xbb\x9c\x5cDemo' + '\n\tRSN:\n'
        self.assertEqual(wifi.parse_scan(output)[0]['ssid'], '网络\\Demo')

    def test_client_failure_clears_candidate_without_persisting(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(wifi, 'RUNTIME', Path(directory)):
            setup = object.__new__(wifi.Setup)
            setup.interface = 'wlan-test'
            setup.stop_children = mock.Mock()
            setup.configure_network = mock.Mock()
            setup.stop = mock.Mock()
            setup.stop.wait.return_value = False
            setup.spawn = mock.Mock(return_value=types.SimpleNamespace(poll=lambda: 1))
            with mock.patch.object(wifi, 'CREDENTIALS', Path(directory) / 'credentials.json'):
                self.assertFalse(setup.connect({'ssid': 'synthetic', 'password': 'synthetic-password'}))
                self.assertFalse(wifi.CREDENTIALS.exists())
                config = (Path(directory) / 'client.conf').read_text()
                self.assertNotIn('synthetic-password', config)
                self.assertIn('scan_ssid=1', config)
            self.assertEqual(setup.stop_children.call_count, 2)

    def test_client_success_keeps_credentials_for_adoption(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(wifi, 'RUNTIME', Path(directory)):
            setup = object.__new__(wifi.Setup)
            setup.interface = 'wlan-test'
            setup.stop_children = mock.Mock()
            setup.configure_network = mock.Mock()
            setup.stop = mock.Mock()
            setup.stop.wait.return_value = False
            setup.spawn = mock.Mock(return_value=types.SimpleNamespace(poll=lambda: None))
            setup.connected = mock.Mock(return_value=True)
            candidate = {'ssid': 'synthetic', 'password': 'synthetic-password'}
            with mock.patch.object(wifi, 'CREDENTIALS', Path(directory) / 'credentials.json'):
                self.assertTrue(setup.connect(candidate))
                self.assertEqual(wifi.read_credentials(), candidate)
                self.assertEqual(setup.stop_children.call_count, 1)

    def test_adopted_device_does_not_start_setup(self):
        with mock.patch.object(wifi, 'IDENTITY') as identity, mock.patch.object(wifi, 'command') as command:
            identity.exists.return_value = True
            wifi.main()
            command.assert_not_called()


class HandoffTests(unittest.TestCase):
    def test_response_is_correlated_nonretained_and_uncached(self):
        from test_control import _load_control_module
        control = _load_control_module()
        plane = object.__new__(control.ControlPlane)
        plane.client = mock.Mock()
        plane.topic_prefix = 'reefy/synthetic-owner'
        plane.current_uuid = 'synthetic-device'
        plane.mode = 'bootstrap'
        plane._claim_command = mock.Mock(side_effect=AssertionError('secret response cached'))
        candidate = {'ssid': 'synthetic', 'password': 'synthetic-password'}
        with mock.patch.object(wifi, 'read_credentials', return_value=candidate):
            for _ in range(2):
                plane._handle_command({'id': 'synthetic-request', 'action': 'wifi_bootstrap_credentials'})
        self.assertEqual(plane.client.publish.call_count, 2)
        args, options = plane.client.publish.call_args
        self.assertEqual(json.loads(args[1]), {'id': 'synthetic-request', 'status': 'success', 'wifi': candidate})
        self.assertEqual(options, {'qos': 1, 'retain': False})
        plane._claim_command.assert_not_called()


@unittest.skipUnless(__import__('sys').platform == 'linux' and __import__('os').geteuid() == 0,
                     'interface-bound HTTP smoke test requires Linux root')
class PortalHttpTests(unittest.TestCase):
    def test_html_forms_escape_ssids_and_reject_bad_tokens(self):
        import http.server
        import threading
        import urllib.error
        import urllib.parse
        import urllib.request
        setup = object.__new__(wifi.Setup)
        setup.interface = 'lo'
        setup.networks = [{'ssid': '<synthetic>$token', 'security': 'secured'}]
        setup.token = 'synthetic-token'
        setup.ssid = 'Reefy-Setup-TEST'
        setup.pending = None
        setup.refresh = False
        setup.lock = threading.Lock()
        page = Path(__file__).resolve().parents[1] / 'rootfs-overlay/usr/share/reefy/wifi-setup.html'
        original = http.server.ThreadingHTTPServer.__init__
        def ephemeral(server, address, *args, **kwargs):
            original(server, (address[0], 0), *args, **kwargs)
        with mock.patch.object(wifi, 'PAGE', page), mock.patch.object(wifi, 'ADDRESS', '127.0.0.1'), \
                mock.patch.object(http.server.ThreadingHTTPServer, '__init__', ephemeral):
            setup.serve()
            thread = threading.Thread(target=setup.server.serve_forever, daemon=True)
            thread.start()
            url = f'http://127.0.0.1:{setup.server.server_port}'
            try:
                with urllib.request.urlopen(url) as response:
                    body = response.read().decode()
                self.assertIn('&lt;synthetic&gt;$token', body)
                self.assertNotIn('<synthetic>', body)
                request = urllib.request.Request(url + '/connect', data=urllib.parse.urlencode({
                    'token': 'wrong', 'ssid': 'synthetic', 'password': 'synthetic-password'}).encode())
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 403)
                self.assertIsNone(setup.pending)
                request = urllib.request.Request(url + '/connect', data=urllib.parse.urlencode({
                    'token': setup.token, 'ssid': 'synthetic', 'password': 'synthetic-password'}).encode())
                with urllib.request.urlopen(request) as response:
                    body = response.read().decode()
                    self.assertEqual(response.status, 202)
                self.assertIn('Setup Wi-Fi will disconnect', body)
                self.assertNotIn('synthetic-password', body)
                self.assertEqual(setup.pending, {'ssid': 'synthetic', 'password': 'synthetic-password'})
            finally:
                setup.server.shutdown()
                setup.server.server_close()
                thread.join(timeout=5)
