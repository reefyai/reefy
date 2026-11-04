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
    def exercise(self, name, timed_out=False, refused=False, confirm_refused=False,
                 unknown_destination=False, storage_changes_default=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = root / 'shim'
            shim.write_text(f'#!{sys.executable}\n' + '''import os, pathlib, sys
root=pathlib.Path(os.environ['WAIT_TEST_ROOT'])
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
if name=='efibootmgr':
 first='1234' if (root/'unknown-destination').exists() else '0001'
 if (root/'storage-changes-default').exists() and int((root/'ticks').read_text())<85: first='0002'
 print('BootCurrent: 0002\\nBootOrder: '+first+',0002\\nBoot0001* reefy-a HD(1,GPT,GUID-A,0x800,0x200000)\\nBoot0002* reefy-b HD(2,GPT,GUID-B,0x200800,0x200000)')
elif name=='findmnt': print('/dev/sda2')
elif name=='blkid': print('GUID-B')
elif name=='sleep':
 p=root/'ticks'; p.write_text(str(int(p.read_text())+1))
elif name=='systemctl':
 ticks=int((root/'ticks').read_text())
 if args[0]=='show': print('activating' if ticks<85 else 'active')
 elif args[0]=='is-failed': sys.exit(1)
 elif args[0]=='is-active': sys.exit(0 if ticks>=85 else 3)
 elif args[0]=='stop': (root/'watchdog-stopped').touch()
elif name=='reefy-efi':
 if (root/'confirm-refused').exists(): sys.exit(1)
 (root/'confirmed').write_text('yes')
''')
            shim.chmod(0o755)
            for command in ('efibootmgr', 'findmnt', 'blkid', 'sleep', 'systemctl', 'reefy-efi'):
                (root / command).symlink_to(shim)
            (root / 'ticks').write_text('0')
            source = (BIN / name).read_text().replace('/proc/sysrq-trigger', str(root / 'reboot'))
            source = source.replace('/run/reefy/storage-recovery-timeout', str(root / 'timed-out'))
            source = source.replace('/run/reefy/storage-recovery-failed', str(root / 'failed'))
            source = source.replace('/usr/lib/reefy/boot-slot-state.sh',
                str(BIN.parent / 'lib/reefy/boot-slot-state.sh'))
            if timed_out or refused:
                (root / 'failed').touch()
            if timed_out:
                (root / 'timed-out').touch()
            if confirm_refused:
                (root / 'confirm-refused').touch()
            if unknown_destination:
                (root / 'unknown-destination').touch()
                (root / 'ticks').write_text('85')
            if storage_changes_default:
                (root / 'storage-changes-default').touch()
            result = subprocess.run(['sh', '-c', source], text=True, capture_output=True,
                                    env={**os.environ, 'WAIT_TEST_ROOT': directory,
                                         'PATH': directory + ':' + os.environ['PATH']})
            result.watchdog_stopped = (root / 'watchdog-stopped').exists()
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

    def test_confirmation_failure_does_not_stop_watchdog(self):
        result, ticks, confirmed, rebooted = self.exercise('reefy-boot-confirm', confirm_refused=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(result.watchdog_stopped)
        self.assertFalse(confirmed)
        self.assertFalse(rebooted)

    def test_unknown_rollback_destination_preserves_diagnostics(self):
        result, ticks, confirmed, rebooted = self.exercise(
            'reefy-boot-watchdog', unknown_destination=True)
        self.assertEqual(result.returncode, 1)
        self.assertGreaterEqual(ticks, 85 + 72)
        self.assertFalse(confirmed)
        self.assertFalse(rebooted)

    def test_confirmation_waits_until_storage_finishes_repairing_entries(self):
        result, ticks, confirmed, rebooted = self.exercise(
            'reefy-boot-confirm', storage_changes_default=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(ticks, 85)
        self.assertTrue(confirmed)
        self.assertFalse(rebooted)

    def test_watchdog_waits_until_storage_finishes_repairing_entries(self):
        result, ticks, confirmed, rebooted = self.exercise(
            'reefy-boot-watchdog', storage_changes_default=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(ticks, 85 + 72)
        self.assertFalse(confirmed)
        self.assertTrue(rebooted)


if __name__ == '__main__':
    unittest.main()
