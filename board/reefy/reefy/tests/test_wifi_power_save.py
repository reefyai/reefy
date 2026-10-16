"""Host-only coverage for the WiFi latency default and setup fallback."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


BOARD = Path(__file__).resolve().parents[1]
SCRIPT = BOARD / 'rootfs-overlay/usr/bin/wifi-setup'


class WifiPowerSaveTests(unittest.TestCase):
    def test_kernel_defaults_to_power_save_off(self):
        config = (BOARD / 'kernel-config').read_text()
        self.assertTrue('# CONFIG_CFG80211_DEFAULT_PS is not set' in config,
                        'kernel must default WiFi power saving to off')
        self.assertFalse('CONFIG_CFG80211_DEFAULT_PS=y' in config)
        self.assertNotIn('CONFIG_CFG80211_DEFAULT_PS=y',
                         (BOARD / 'kernel-config-wifi').read_text())

    def run_setup(self, iface='wlan-test0', unsupported=False, missing=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = root / 'wifi.conf'
            unrelated = root / 'unrelated.conf'
            unrelated.write_text('preserve unrelated configuration\n')
            # Redirect configuration output only; all network tools are fakes.
            script = root / 'wifi-setup'
            script.write_text(SCRIPT.read_text().replace(
                'CONF_FILE="/etc/wpa_supplicant/wpa_supplicant-${IFACE}.conf"',
                'CONF_FILE="$TEST_CONF"'))
            commands = {
                'ip': 'case "$*" in "link show "*) exit "$MISSING";; esac\n',
                'iw': '''printf '%s\n' "iw $*" >> "$TEST_LOG"
case "$*" in
  *"set power_save off") exit "$UNSUPPORTED";;
  *" link") echo Connected;;
esac
''',
                'systemctl': 'printf "%s\\n" "systemctl $*" >> "$TEST_LOG"\n',
                'networkctl': 'printf "%s\\n" "networkctl $*" >> "$TEST_LOG"\n',
                'sleep': 'exit 0\n',
                'wpa_passphrase': 'printf "network={\\n  psk=synthetic-hash\\n  #psk=secret\\n}\\n"\n',
            }
            for name, body in commands.items():
                executable = root / name
                executable.write_text('#!/bin/sh\n' + body)
                executable.chmod(0o755)
            # The device uses BusyBox sed; macOS sed has a different -i syntax.
            sed = root / 'sed'
            sed.write_text('#!' + sys.executable + '\n'
                           'from pathlib import Path\nimport sys\n'
                           'p = Path(sys.argv[-1])\n'
                           'p.write_text("".join(line for line in '
                           'p.read_text().splitlines(True) if "#psk=" not in line))\n')
            sed.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       TEST_CONF=str(conf), TEST_LOG=str(root / 'calls'),
                       UNSUPPORTED=str(int(unsupported)), MISSING=str(int(missing)))
            result = subprocess.run(['sh', str(script), 'TestNetwork',
                                     'test-password', iface], env=env,
                                    capture_output=True, text=True, timeout=5)
            log = (root / 'calls').read_text() if (root / 'calls').exists() else ''
            config = conf.read_text() if conf.exists() else None
            self.assertEqual(unrelated.read_text(), 'preserve unrelated configuration\n')
            return result, log, config

    def test_disables_power_save_on_each_selected_interface(self):
        for iface in ('wlan-test0', 'wlp-test1', 'wlx-test2'):
            with self.subTest(iface=iface):
                result, log, config = self.run_setup(iface)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'iw dev {iface} set power_save off\n', log)
                self.assertIn(f'networkctl reconfigure {iface}\n', log)
                self.assertIn('psk=synthetic-hash', config)
                self.assertNotIn('#psk=', config)

    def test_unsupported_power_save_does_not_block_connection(self):
        result, log, config = self.run_setup(unsupported=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('power saving', result.stderr)
        self.assertIn('networkctl reconfigure wlan-test0', log)
        self.assertIn('psk=synthetic-hash', config)

    def test_missing_interface_does_not_change_configuration(self):
        result, log, config = self.run_setup(missing=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('set power_save', log)
        self.assertNotIn('systemctl', log)
        self.assertIsNone(config)


if __name__ == '__main__':
    unittest.main()
