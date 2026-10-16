"""Exercise early WiFi state loading without a network or real credentials."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = (Path(__file__).resolve().parents[1]
          / 'rootfs-overlay/usr/bin/reefy-wifi-early')


class WifiEarlyTests(unittest.TestCase):
    def run_early(self, state=None, raw=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v1, v2 = root / 'desired-state.json', root / 'desired-state-v2.json'
            if raw is not None:
                v2.write_text(raw)
            elif state is not None:
                (v2 if state.get('schema_version') == 2 else v1).write_text(json.dumps(state))
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            helper = root / 'wifi-setup'
            helper.write_text('#!' + sys.executable + '\n'
                              'import json, os, sys\nfrom pathlib import Path\n'
                              'Path(os.environ["TEST_CALL"]).write_text(json.dumps(sys.argv[1:]))\n')
            helper.chmod(0o755)
            source = SCRIPT.read_text().replace(
                '/mnt/reefy-data/state/desired-state-v2.json', str(v2)).replace(
                '/mnt/reefy-data/state/desired-state.json', str(v1))
            call = root / 'call.json'
            result = subprocess.run(
                ['sh', '-c', source], capture_output=True, text=True, timeout=10,
                env=dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                         TEST_CALL=str(call)))
            for name, content in before.items():
                self.assertEqual((root / name).read_bytes(), content)
            return result, json.loads(call.read_text()) if call.exists() else None

    def test_valid_v1_and_v2_preserve_connection_arguments(self):
        wifi = {'ssid': 'Synthetic "network"', 'password': 'synthetic-private-password'}
        for state in ({'wifi': wifi}, {'schema_version': 2, 'host': {'wifi': wifi}}):
            with self.subTest(version=state.get('schema_version', 1)):
                result, call = self.run_early(state)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(call, [wifi['ssid'], wifi['password']])
                self.assertEqual(result.stderr, '')
                self.assertNotIn(wifi['password'], result.stdout)

    def test_missing_state_and_no_wifi_skip_without_errors(self):
        for state in (None, {}, {'schema_version': 2, 'host': {}}):
            with self.subTest(state=state):
                result, call = self.run_early(state)
                self.assertEqual(result.returncode, 0)
                self.assertIsNone(call)
                self.assertEqual(result.stderr, '')

    def test_invalid_json_reports_type_without_private_contents(self):
        result, call = self.run_early(raw='synthetic-private-value invalid json')
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(call)
        self.assertIn('JSONDecodeError', result.stderr)
        self.assertNotIn('synthetic-private-value', result.stdout + result.stderr)

    def test_wrong_shape_reports_type_without_private_contents(self):
        result, call = self.run_early(raw='{"wifi": "synthetic-private-value"}')
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(call)
        self.assertIn('AttributeError', result.stderr)
        self.assertNotIn('synthetic-private-value', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
