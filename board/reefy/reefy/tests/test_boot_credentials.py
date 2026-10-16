"""Run the boot credential helper with synthetic state and safe command fakes."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = (Path(__file__).resolve().parents[1]
          / 'rootfs-overlay/usr/bin/boot-reefy-init.sh')


class BootCredentialTests(unittest.TestCase):
    def test_applies_credentials_without_tracing_password_or_hash(self):
        for tracing in (False, True):
            with self.subTest(inherited_tracing=tracing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / 'state'
                state.mkdir()
                secret = 'synthetic-device-password'
                hashed = 'synthetic-password-hash'
                (state / 'device_password').write_text(secret)
                shadow = root / 'shadow'
                shadow.write_text('root:old:0:0:0:0:0:0:\nother:preserved:1:2:3:4:5:6:\n')
                mkpasswd = root / 'mkpasswd'
                mkpasswd.write_text('#!' + sys.executable + '\n'
                                    'import os, sys\n'
                                    'assert sys.argv[1] == os.environ["TEST_PASSWORD"]\n'
                                    'print(os.environ["TEST_HASH"])\n')
                sed = root / 'sed'
                sed.write_text('#!' + sys.executable + '\n'
                               'import os, sys\nfrom pathlib import Path\n'
                               'p = Path(sys.argv[-1])\n'
                               'assert sys.argv[2] == "s|^root:[^:]*:|root:" + os.environ["TEST_HASH"] + ":|"\n'
                               'p.write_text(p.read_text().replace("root:old:", "root:" + os.environ["TEST_HASH"] + ":"))\n')
                for command in (mkpasswd, sed):
                    command.chmod(0o755)
                source = SCRIPT.read_text().split('# Main execution')[0]
                source = source.replace('REEFY_DATA_MNT="/mnt/reefy-data"',
                                        'REEFY_DATA_MNT="$TEST_ROOT"')
                source = source.replace('DEVICE_USERS="root reefy"', 'DEVICE_USERS="root"')
                source = source.replace('/etc/shadow', str(shadow))
                source += '\nsetup_device_credentials\n'
                result = subprocess.run(
                    ['bash', *(['-x'] if tracing else []), '-c', source],
                    env=dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                             TEST_ROOT=str(root), TEST_PASSWORD=secret, TEST_HASH=hashed),
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0)
                self.assertIn('[reefy] Device credentials applied', result.stdout)
                self.assertEqual(shadow.read_text(),
                                 f'root:{hashed}:0:0:0:0:0:0:\nother:preserved:1:2:3:4:5:6:\n')
                self.assertEqual((state / 'device_password').read_text(), secret)
                for value in (secret, hashed):
                    self.assertNotIn(value, result.stdout)
                    self.assertNotIn(value, result.stderr)


if __name__ == '__main__':
    unittest.main()
