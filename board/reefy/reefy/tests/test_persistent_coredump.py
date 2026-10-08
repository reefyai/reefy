import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy import persistent_coredump as core


class CoreStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name in ('DATA', 'RUNTIME', 'DESTINATION'):
            p = patch.object(core, name, root / name)
            p.start().mkdir()
            self.addCleanup(p.stop)
        p = patch.object(core, 'CONFIG', root / 'config/budget.conf')
        p.start()
        self.addCleanup(p.stop)
        self.commands = []
        self.mount = {'fstype': 'xfs', 'options': 'rw,noatime'}
        p = patch.object(core, 'run', side_effect=self.command)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(core.os.path, 'ismount', return_value=False)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(core.os, 'statvfs', return_value=SimpleNamespace(
            f_bavail=100 * 1024 ** 3, f_frsize=1))
        p.start()
        self.addCleanup(p.stop)

    def command(self, args):
        self.commands.append(args)
        return SimpleNamespace(stdout=json.dumps({'filesystems': [self.mount]}))

    def test_budget_bounds_dump_pool_and_preserves_free_space(self):
        self.assertIsNone(core.budget(core.KEEP_FREE))
        self.assertIsNone(core.budget(core.KEEP_FREE + 1))
        self.assertEqual(core.budget(100 * 1024 ** 3), (4 * 1024 ** 3, 2 * 1024 ** 3))
        total, individual = core.budget(10 * 1024 ** 3)
        self.assertEqual(total, 512 * 1024 ** 2)
        self.assertLessEqual(individual, total // 2)

    def test_early_spool_is_size_bounded_and_private(self):
        core.early()
        self.assertIn('size=256M,mode=0700,nodev,nosuid,noexec', self.commands[0])
        self.assertEqual(core.RUNTIME.stat().st_mode & 0o777, 0o700)

    def test_completed_early_dumps_are_preserved_and_temporary_files_ignored(self):
        source = core.RUNTIME / 'core.synthetic'
        source.write_bytes(b'\x7fELFsynthetic retained payload')
        temporary = core.RUNTIME / '.unfinished'
        temporary.write_bytes(b'in progress')
        core.attach()
        final = core.DATA / 'coredumps/core.synthetic'
        self.assertEqual(final.read_bytes(), b'\x7fELFsynthetic retained payload')
        self.assertFalse(source.exists())
        self.assertTrue(temporary.exists())
        self.assertIn('ExternalSizeMax=2147483648', core.CONFIG.read_text())
        self.assertEqual(final.parent.stat().st_mode & 0o777, 0o700)

    def test_failed_transfer_keeps_early_evidence(self):
        source = core.RUNTIME / 'core.synthetic'
        source.write_bytes(b'original')
        destination = core.DATA / 'coredumps'
        destination.mkdir()
        with patch.object(core.shutil, 'copy2', side_effect=OSError('full disk')):
            with self.assertRaises(OSError):
                core.migrate(destination)
        self.assertEqual(source.read_bytes(), b'original')

    def test_symlinks_and_ambiguous_prior_dumps_are_not_overwritten(self):
        destination = core.DATA / 'coredumps'
        destination.mkdir()
        (core.RUNTIME / 'core.synthetic').write_bytes(b'new')
        (destination / 'core.synthetic').write_bytes(b'old')
        (core.RUNTIME / 'core.link').symlink_to(destination / 'core.synthetic')
        core.migrate(destination)
        self.assertEqual((destination / 'core.synthetic').read_bytes(), b'old')
        self.assertTrue((core.RUNTIME / 'core.synthetic').exists())
        self.assertFalse((destination / 'core.link').exists())

    def test_ram_readonly_and_foreign_mounts_are_refused(self):
        for filesystem, options in [('tmpfs', 'rw'), ('overlay', 'rw'), ('xfs', 'ro')]:
            self.mount = {'fstype': filesystem, 'options': options}
            core.attach()
            self.assertFalse(core.CONFIG.exists())
        self.mount = {'fstype': 'xfs', 'options': 'rw'}
        with patch.object(core.os.path, 'ismount', return_value=True), \
                patch.object(core.os.path, 'samefile', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'unrelated'):
                core.attach()

    def test_late_worker_is_drained_on_next_attachment(self):
        destination = core.DATA / 'coredumps'
        destination.mkdir()
        (core.RUNTIME / 'core.late').write_bytes(b'late worker')
        with patch.object(core.os.path, 'ismount', return_value=True), \
                patch.object(core.os.path, 'samefile', side_effect=lambda a, b: b == core.DESTINATION):
            core.attach()
        self.assertEqual((destination / 'core.late').read_bytes(), b'late worker')

    def test_stopping_restores_ram_capture_and_runtime_budget(self):
        (core.DATA / 'coredumps').mkdir()
        core.CONFIG.parent.mkdir()
        core.CONFIG.write_text('[Coredump]')
        with patch.object(core.os.path, 'ismount', return_value=True), \
                patch.object(core.os.path, 'samefile', side_effect=lambda a, b: b == core.DESTINATION):
            core.stop()
        self.assertEqual(self.commands[-1], ['umount', str(core.DESTINATION)])
        self.assertFalse(core.CONFIG.exists())


class CoreMemoryPolicyTests(unittest.TestCase):
    def test_default_service_filter_excludes_private_memory(self):
        root = Path(__file__).resolve().parents[1] / 'rootfs-overlay'
        import configparser
        policy = configparser.ConfigParser()
        policy.read(root / 'etc/systemd/system/service.d/reefy-coredump-filter.conf')
        self.assertEqual(policy['Service']['CoredumpFilter'], 'elf-headers')
