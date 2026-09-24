"""Boot rollback must not interrupt a long-running storage operation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BIN = Path(__file__).parents[1] / 'rootfs-overlay/usr/bin'


class StorageWaitTests(unittest.TestCase):
    def exercise(self, name, timed_out=False, refused=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = root / 'shim'
            shim.write_text(f'#!{sys.executable}\n' + '''import os, pathlib, sys
root=pathlib.Path(os.environ['WAIT_TEST_ROOT'])
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
if name=='efibootmgr':
 print('BootCurrent: 0002\\nBootOrder: 0001,0002')
elif name=='sleep':
 p=root/'ticks'; p.write_text(str(int(p.read_text())+1))
elif name=='systemctl':
 ticks=int((root/'ticks').read_text())
 if args[0]=='show': print('activating' if ticks<85 else 'active')
 elif args[0]=='is-failed': sys.exit(1)
 elif args[0]=='is-active': sys.exit(0 if ticks>=85 else 3)
elif name=='reefy-efi':
 (root/'confirmed').write_text('yes')
''')
            shim.chmod(0o755)
            for command in ('efibootmgr', 'sleep', 'systemctl', 'reefy-efi'):
                (root / command).symlink_to(shim)
            (root / 'ticks').write_text('0')
            source = (BIN / name).read_text().replace('/proc/sysrq-trigger', str(root / 'reboot'))
            source = source.replace('/run/reefy/storage-recovery-timeout', str(root / 'timed-out'))
            source = source.replace('/run/reefy/storage-recovery-failed', str(root / 'failed'))
            if timed_out or refused:
                (root / 'failed').touch()
            if timed_out:
                (root / 'timed-out').touch()
            result = subprocess.run(['sh', '-c', source], text=True, capture_output=True,
                                    env={**os.environ, 'WAIT_TEST_ROOT': directory,
                                         'PATH': directory + ':' + os.environ['PATH']})
            return result, int((root / 'ticks').read_text()), (root / 'confirmed').exists(), (root / 'reboot').exists()

    def test_confirmation_waits_beyond_old_deadline_for_storage(self):
        result, ticks, confirmed, rebooted = self.exercise('reefy-boot-confirm')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(ticks, 85)  # 425 simulated seconds, beyond 300.
        self.assertTrue(confirmed)
        self.assertFalse(rebooted)

    def test_rollback_budget_does_not_count_storage_work(self):
        result, ticks, confirmed, rebooted = self.exercise('reefy-boot-watchdog')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(ticks, 85 + 72)
        self.assertTrue(rebooted)  # Normal rollback still happens after storage exits.
        self.assertFalse(confirmed)

    def test_timeout_diagnostics_neither_confirm_nor_rollback(self):
        for name in ('reefy-boot-watchdog', 'reefy-boot-confirm'):
            with self.subTest(name=name):
                result, ticks, confirmed, rebooted = self.exercise(name, timed_out=True)
                self.assertEqual(result.returncode, 1)
                self.assertFalse(confirmed)
                self.assertFalse(rebooted)
                self.assertEqual(ticks, 0)

    def test_repair_refusal_does_not_boot_older_repair_code(self):
        result, ticks, confirmed, rebooted = self.exercise('reefy-boot-watchdog', refused=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(ticks, 0)
        self.assertFalse(confirmed)
        self.assertFalse(rebooted)


if __name__ == '__main__':
    unittest.main()
