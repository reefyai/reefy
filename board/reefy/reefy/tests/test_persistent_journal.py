import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy import persistent_journal as journal


class PersistentJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'data'
        self.data.mkdir()
        self.destination = Path(self.temp.name) / 'var/log/journal'
        self.commands = []
        self.filesystem = 'xfs'
        self.options = 'rw,noatime'
        self.bound = False
        self.fail_flush = False
        for attribute, value in [('DATA', self.data), ('JOURNAL', self.destination), ('BUDGET_CONFIG', Path(self.temp.name) / 'budget.conf')]:
            p = patch.object(journal, attribute, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(journal.os, 'statvfs')
        stat = p.start().return_value
        stat.f_bavail = 100 * 1024 ** 3
        stat.f_frsize = 1
        self.addCleanup(p.stop)
        p = patch.object(journal, 'run', side_effect=self.command)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(journal.os.path, 'ismount', side_effect=lambda _: self.bound)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(journal.os.path, 'samefile', return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def command(self, args):
        self.commands.append(args)
        if args[0] == 'findmnt':
            class Result:
                stdout = json.dumps({'filesystems': [{
                    'fstype': self.filesystem, 'options': self.options}]})
            return Result()
        if args[0] == 'mount':
            self.bound = True
        if args == ['journalctl', '--flush'] and self.fail_flush:
            raise RuntimeError('flush failure')
        if args[0] == 'umount':
            self.bound = False

    def test_flushes_early_journal_after_binding_data(self):
        journal.start()
        self.assertEqual(self.commands[1:], [
            ['systemctl', 'restart', 'systemd-journald.service'],
            ['mount', '--bind', str(self.data / 'journal'), str(self.destination)],
            ['journalctl', '--flush'],
        ])
        self.assertEqual((self.data / 'journal').stat().st_mode & 0o7777, 0o750)

    def test_ram_and_readonly_data_do_not_switch_logging(self):
        for filesystem, options in [('tmpfs', 'rw'), ('overlay', 'rw'), ('xfs', 'ro')]:
            self.filesystem, self.options = filesystem, options
            self.commands.clear()
            journal.start()
            self.assertEqual(len(self.commands), 1)

    def test_flush_failure_relinquishes_before_unmount(self):
        self.fail_flush = True
        with self.assertRaises(RuntimeError):
            journal.start()
        self.assertEqual(self.commands[-2:], [
            ['journalctl', '--relinquish-var'], ['umount', str(self.destination)]])

    def test_runtime_budget_sorts_after_static_defaults(self):
        self.assertGreater('zz-reefy-budget.conf', 'reefy.conf')

    def test_budget_uses_available_space_and_absolute_cap(self):
        journal.configure_budget()
        self.assertIn('SystemMaxUse=10737418240', journal.BUDGET_CONFIG.read_text())
        with patch.object(journal.os, 'statvfs') as stat:
            stat.return_value.f_bavail = 400 * 1024 ** 3
            stat.return_value.f_frsize = 1
            journal.configure_budget()
        self.assertIn('SystemMaxUse=17179869184', journal.BUDGET_CONFIG.read_text())

    def test_static_config_preserves_runtime_limits(self):
        config = (Path(__file__).parent.parent / 'rootfs-overlay/etc/systemd/journald.conf.d/reefy.conf').read_text()
        self.assertNotIn('RuntimeMaxUse=', config)
        self.assertNotIn('RuntimeKeepFree=', config)
        self.assertNotIn('MaxRetentionSec=', config)
        journal.configure_budget()
        self.assertIn('MaxRetentionSec=90day', journal.BUDGET_CONFIG.read_text())

    def test_low_space_preserves_ram_logging(self):
        with patch.object(journal.os, 'statvfs') as stat:
            stat.return_value.f_bavail = journal.KEEP_FREE_BYTES
            stat.return_value.f_frsize = 1
            journal.start()
        self.assertEqual(len(self.commands), 1)
        self.assertFalse(journal.BUDGET_CONFIG.exists())

    def test_stop_closes_journal_before_data_unmount(self):
        self.bound = True
        journal.stop()
        self.assertEqual(self.commands, [
            ['journalctl', '--relinquish-var'], ['umount', str(self.destination)]])

    def test_unrelated_mount_is_not_detached(self):
        self.bound = True
        with patch.object(journal.os.path, 'samefile', return_value=False):
            with self.assertRaises(RuntimeError):
                journal.start()
            with self.assertRaises(RuntimeError):
                journal.stop()
        self.assertEqual(len(self.commands), 1)

    def test_repeated_start_reuses_binding(self):
        self.bound = True
        journal.start()
        self.assertEqual(self.commands[-1], ['journalctl', '--flush'])
        self.assertFalse(any(c[0] == 'mount' for c in self.commands))

    def test_shutdown_without_binding_is_noop(self):
        journal.stop()
        self.assertEqual(self.commands, [])


if __name__ == '__main__':
    unittest.main()
