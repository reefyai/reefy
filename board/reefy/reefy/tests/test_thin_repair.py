"""Native repair must not replace authoritative mappings with guessed history."""
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'rootfs-overlay/usr/lib/reefy'))
from reefy import thin_repair


class ThinRepairTests(unittest.TestCase):
    def test_unreadable_current_roots_never_run_repair(self):
        with patch.object(thin_repair, 'fingerprint', side_effect=RuntimeError('lost root')), \
                patch.object(thin_repair.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'lost root'):
                thin_repair.repair('/input', '/spare')
            self.assertEqual(run.call_count, 1)
            self.assertIn('--super-block-only', run.call_args.args[0])

    def test_bad_superblock_never_runs_repair(self):
        with patch.object(thin_repair.subprocess, 'run',
                          side_effect=subprocess.CalledProcessError(1, 'thin_check')) as run, \
                patch.object(thin_repair, 'fingerprint') as dump:
            with self.assertRaises(subprocess.CalledProcessError):
                thin_repair.repair('/input', '/spare')
            self.assertEqual(run.call_count, 1)
            dump.assert_not_called()

    def test_different_mapping_history_refuses_swap(self):
        with patch.object(thin_repair, 'fingerprint', side_effect=['current', 'older']), \
                patch.object(thin_repair.subprocess, 'run'):
            with self.assertRaisesRegex(RuntimeError, 'refusing metadata swap'):
                thin_repair.repair('/input', '/spare')

    def test_native_repair_error_is_not_success(self):
        with patch.object(thin_repair, 'fingerprint', return_value='current'), \
                patch.object(thin_repair.subprocess, 'run', side_effect=[None,
                    subprocess.CalledProcessError(1, 'thin_repair')]):
            with self.assertRaises(subprocess.CalledProcessError):
                thin_repair.repair('/input', '/spare')

    def test_matching_mapping_dump_allows_checked_repair(self):
        with patch.object(thin_repair, 'fingerprint', return_value='current') as dump, \
                patch.object(thin_repair.subprocess, 'run') as run:
            thin_repair.repair('/input', '/spare')
            self.assertEqual([c.args[0] for c in run.call_args_list], [
                ['/usr/sbin/thin_check', '--super-block-only', '/input'],
                ['/usr/sbin/thin_repair', '-i', '/input', '-o', '/spare'],
                ['/usr/sbin/thin_check', '/spare']])
            self.assertEqual([c.args[0] for c in dump.call_args_list], ['/input', '/spare'])

    def test_partial_dump_with_failed_exit_is_not_a_fingerprint(self):
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.BytesIO(b'<superblock>partial')
        process.wait.return_value = 1
        with patch.object(thin_repair.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, 'refusing historical'):
                thin_repair.fingerprint('/input')


if __name__ == '__main__':
    unittest.main()
