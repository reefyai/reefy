"""Exercise timeout decisions, durable guard failures and real process groups."""
from contextlib import nullcontext
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'rootfs-overlay/usr/lib/reefy'))
from reefy import storage_boot as boot


class BackstopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, path in (('RUN', root / 'run'), ('MARKER', root / 'marker.json'),
                           ('PARTIAL', root / 'marker.partial')):
            mock = patch.object(boot, name, path)
            mock.start()
            self.addCleanup(mock.stop)

    def test_healthy_exit_does_not_write_or_interrupt(self):
        with patch.object(boot, 'preserve') as persist, patch.object(boot, 'interrupt') as stop:
            self.assertEqual(boot.supervise([sys.executable, '-c', 'pass'], deadline=5), 0)
            persist.assert_not_called()
            stop.assert_not_called()
        self.assertFalse(boot.RUN.exists())

    def test_existing_and_partial_guards_prevent_any_recovery(self):
        for marker in (boot.MARKER, boot.PARTIAL):
            with self.subTest(marker=marker):
                marker.write_text('incomplete or full evidence')
                with patch.object(boot.subprocess, 'Popen') as launch:
                    self.assertEqual(boot.supervise(['must-not-execute']), 1)
                    launch.assert_not_called()
                self.assertTrue((boot.RUN / 'storage-recovery-failed').exists())
                marker.unlink()

    @unittest.skipUnless(sys.platform == 'linux', 'requires Linux /proc')
    def test_timeout_stops_real_process_group_and_releases_diagnostics(self):
        with patch.object(boot, 'report', return_value=b'{}'), \
             patch.object(boot, 'preserve', return_value=True) as persist, \
             patch.object(boot, 'detach_data', return_value=True), \
             patch.object(boot, 'reboot_current') as reboot:
            result = boot.supervise([sys.executable, '-c', 'import time; time.sleep(60)'], deadline=.2)
            self.assertEqual(result, 1)
            persist.assert_called_once_with(b'{}')
            reboot.assert_not_called()
            self.assertTrue((boot.RUN / 'storage-recovery-timeout').exists())

    @unittest.skipUnless(sys.platform == 'linux', 'requires Linux /proc')
    def test_surviving_descendant_is_not_mistaken_for_stopped_repair(self):
        process = subprocess.Popen([sys.executable, '-c',
            'import os,signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); '
            'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
            'pid=os.fork(); time.sleep(60) if pid==0 else None'], start_new_session=True)
        try:
            process.wait(timeout=5)
            self.assertFalse(boot.interrupt(process, grace=.05))
        finally:
            import os
            os.killpg(process.pid, signal.SIGKILL)

    def test_stuck_writer_reboots_only_after_durable_guard(self):
        class HeldForSafety(Exception):
            pass
        for durable in (True, False):
            with self.subTest(durable=durable), \
                 patch.object(boot, 'report', return_value=b'{}'), \
                 patch.object(boot, 'preserve', return_value=durable), \
                 patch.object(boot, 'interrupt', return_value=False), \
                 patch.object(boot, 'reboot_current') as reboot, \
                 patch.object(boot.time, 'sleep', side_effect=HeldForSafety):
                process = Mock(pid=123)
                process.wait.side_effect = subprocess.TimeoutExpired('fixture', .01)
                with patch.object(boot.subprocess, 'Popen', return_value=process):
                    with self.assertRaises(HeldForSafety):
                        boot.supervise(['fixture'], deadline=.01)
                self.assertEqual(reboot.call_count, int(durable))

    def test_failed_unmount_requires_reboot_not_consumer_start(self):
        with patch.object(boot, 'call', side_effect=[(0, '/\n/mnt/reefy-data\n'),
                                                   RuntimeError('busy')]):
            self.assertFalse(boot.detach_data())

    def test_detaches_nested_mounts_before_primary(self):
        with patch.object(boot, 'call', side_effect=[
                (0, '/\n/mnt/reefy\n/mnt/reefy-data\n/mnt/reefy-data/state\n'),
                (0, ''), (0, '')]) as call:
            self.assertTrue(boot.detach_data())
        self.assertEqual(call.call_args_list[1].args[0], ['umount', '/mnt/reefy-data/state'])
        self.assertEqual(call.call_args_list[2].args[0], ['umount', '/mnt/reefy-data'])

    def test_persistence_failure_forbids_reboot(self):
        with patch.object(boot, 'call', side_effect=RuntimeError('ESP unavailable')):
            self.assertFalse(boot.preserve(b'{}'))
        self.assertEqual((boot.RUN / 'storage-timeout.json').read_bytes(), b'{}')

    def test_unicode_report_remains_bounded_valid_json(self):
        with patch.object(boot, 'group_members', return_value=[]), \
             patch.object(boot, 'call', return_value=(0, '\u2603' * 16000)):
            payload = boot.report(10800, 123)
        self.assertLessEqual(len(payload), boot.MAX_REPORT)
        self.assertEqual(json.loads(payload)['elapsed_seconds'], 10800)

    def test_persist_writes_first_evidence_and_refuses_overwrite(self):
        source = Path(self.temp.name) / 'source.json'
        source.write_bytes(b'{"reason":"synthetic timeout"}')
        with patch('reefy.vg_recovery.writable_esp', return_value=nullcontext()), \
             patch.object(boot, 'call', return_value=(0, '')):
            boot.persist(source)
            self.assertEqual(boot.MARKER.read_bytes(), source.read_bytes())
            self.assertFalse(boot.PARTIAL.exists())
            with self.assertRaisesRegex(RuntimeError, 'prior timeout evidence'):
                boot.persist(source)


    def test_reboot_refuses_failed_slot_selection(self):
        with patch.object(boot, 'call', side_effect=[
                (0, 'reefy-b\n'), RuntimeError('BootNext verification failed')]), \
             patch.object(Path, 'write_text') as write:
            with self.assertRaisesRegex(RuntimeError, 'BootNext'):
                boot.reboot_current()
            write.assert_not_called()

    def test_reboot_uses_mounted_slot_and_existing_verified_helper(self):
        with patch.object(boot, 'call', side_effect=[(0, 'reefy-b\n'), (0, '')]) as call, \
             patch.object(Path, 'write_text') as write:
            boot.reboot_current()
        self.assertEqual(call.call_args_list[1].args[0], ['reefy-efi', 'set-next', 'b'])
        write.assert_called_once_with('b')

    def test_reboot_refuses_unknown_boot_partition(self):
        with patch.object(boot, 'call', return_value=(0, 'unknown\n')) as call, \
             patch.object(Path, 'write_text') as write:
            with self.assertRaisesRegex(RuntimeError, 'cannot identify'):
                boot.reboot_current()
            self.assertEqual(call.call_count, 1)
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
