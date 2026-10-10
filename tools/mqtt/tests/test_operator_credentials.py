"""Exercise certificate generation, upgrades, and command credential selection."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1]


class OperatorCredentialsTest(unittest.TestCase):
    def test_setup_upgrade_and_command_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'server'
            certs = output / 'certs'

            def setup(*extra):
                subprocess.run(['bash', str(TOOLS / 'setup-mqtt-server.sh'),
                                '-d', '127.0.0.1', '-o', str(output), *extra],
                               check=True, capture_output=True)

            setup()
            ca = (certs / 'ca.crt').read_bytes()
            bootstrap = (certs / 'bootstrap.crt').read_bytes()
            bundle = {p.name: p.read_bytes()
                      for p in (output / 'usb-bundle/mqtt').iterdir()}
            self.assertNotIn('admin.key', bundle)
            self.assertNotIn('admin.crt', bundle)
            self.assertEqual((certs / 'admin.key').stat().st_mode & 0o777, 0o600)
            subject = subprocess.check_output([
                'openssl', 'x509', '-in', str(certs / 'admin.crt'),
                '-noout', '-subject', '-nameopt', 'RFC2253'], text=True)
            self.assertIn('CN=reefy-admin', subject)

            # Upgrade a pre-admin installation with a stale permissive ACL.
            (certs / 'admin.crt').unlink()
            (certs / 'admin.key').unlink()
            acl = output / 'broker-config/acl.conf'
            acl.write_text('{allow, all}.\n')
            setup('--skip-certs')
            self.assertEqual((certs / 'ca.crt').read_bytes(), ca)
            self.assertEqual((certs / 'bootstrap.crt').read_bytes(), bootstrap)
            self.assertEqual({p.name: p.read_bytes()
                              for p in (output / 'usb-bundle/mqtt').iterdir()}, bundle)
            rules = acl.read_text()
            self.assertNotIn('{user, "bootstrap"}, publish, ["reefy/devices/+/commands"]', rules)
            self.assertNotIn('{user, "bootstrap"}, subscribe, ["reefy/devices/+/status"]', rules)
            self.assertIn('{user, "reefy-admin"}, publish, ["reefy/devices/+/commands"]', rules)
            self.assertIn('{user, "reefy-admin"}, subscribe, ["reefy/devices/+/status"]', rules)
            self.assertIn('reefy/devices/${username}/#', rules)
            admin = (certs / 'admin.crt').read_bytes()
            setup('--skip-certs')
            self.assertEqual((certs / 'admin.crt').read_bytes(), admin)

            # Execute the actual senders and capture their MQTT invocation.
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            capture = root / 'args'
            stub = bin_dir / 'mosquitto_pub'
            stub.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$MQTT_TEST_CAPTURE"\n')
            stub.chmod(0o755)
            env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}',
                       MQTT_TEST_CAPTURE=str(capture))
            for sender, fixture in [('send-playbook.sh', 'test-playbook.yml'),
                                    ('send-mcl.sh', 'test-config.mcl')]:
                command = ['bash', str(TOOLS / sender), '-c', str(certs),
                           '-d', 'test-device', str(TOOLS / fixture)]
                subprocess.run(command, env=env, check=True, capture_output=True)
                args = capture.read_text().splitlines()
                self.assertEqual(args[args.index('--cert') + 1], str(certs / 'admin.crt'))
                self.assertEqual(args[args.index('--key') + 1], str(certs / 'admin.key'))
                self.assertEqual(args[args.index('-t') + 1], 'reefy/devices/test-device/commands')

            (certs / 'admin.crt').unlink()
            (certs / 'admin.key').unlink()
            for sender, fixture in [('send-playbook.sh', 'test-playbook.yml'),
                                    ('send-mcl.sh', 'test-config.mcl')]:
                capture.unlink(missing_ok=True)
                result = subprocess.run(['bash', str(TOOLS / sender), '-c', str(certs),
                                         '-d', 'test-device', str(TOOLS / fixture)],
                                        env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('bootstrap credentials cannot send commands', result.stderr)
                self.assertFalse(capture.exists())

            # Device-specific callers retain their existing credential selection.
            # MQTT is stubbed here; actual ACL enforcement runs in the CI broker check.
            (certs / 'device.crt').write_bytes(bootstrap)
            (certs / 'device.key').write_bytes((certs / 'bootstrap.key').read_bytes())
            for sender, fixture in [('send-playbook.sh', 'test-playbook.yml'),
                                    ('send-mcl.sh', 'test-config.mcl')]:
                subprocess.run(['bash', str(TOOLS / sender), '-c', str(certs),
                                '-d', 'test-device', str(TOOLS / fixture)],
                               env=env, check=True, capture_output=True)
                args = capture.read_text().splitlines()
                self.assertEqual(args[args.index('--cert') + 1], str(certs / 'device.crt'))

            # A full regeneration must not reuse an admin signed by the old CA.
            setup('--skip-certs')
            old_admin = (certs / 'admin.crt').read_bytes()
            setup()
            self.assertNotEqual((certs / 'admin.crt').read_bytes(), old_admin)
            subprocess.run(['openssl', 'verify', '-CAfile', str(certs / 'ca.crt'),
                            str(certs / 'admin.crt')], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
