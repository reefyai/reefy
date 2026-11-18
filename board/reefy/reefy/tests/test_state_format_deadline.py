"""New state filesystems must finish formatting before adoption can continue."""
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy import storage


class StateFormatDeadlineTests(unittest.TestCase):
    def provision(self, duration=75, failure=False):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            if command[0] == 'vgs':
                return SimpleNamespace(returncode=0, stdout=str(32 * 1024**3), stderr='')
            if command[0] == 'mkfs.xfs':
                if duration > kwargs['timeout']:
                    raise subprocess.TimeoutExpired(command, kwargs['timeout'])
                return SimpleNamespace(returncode=1 if failure else 0, stdout='', stderr='synthetic format failure')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(storage.os.path, 'exists', return_value=False), \
                patch.object(storage.subprocess, 'run', side_effect=run):
            storage.Storage()._ensure_state_lv()
        return calls

    def test_cold_format_finishes_within_existing_data_format_allowance(self):
        calls = self.provision()
        formats = [(cmd, kw) for cmd, kw in calls if cmd[0] == 'mkfs.xfs']
        self.assertEqual(len(formats), 1)
        self.assertIn('-K', formats[0][0])
        self.assertEqual(formats[0][1]['timeout'], 120)

    def test_exhausted_format_is_fatal_without_replay(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.provision(duration=121)

    def test_failed_format_is_fatal(self):
        with self.assertRaisesRegex(RuntimeError, 'state LV mkfs.xfs failed'):
            self.provision(duration=0, failure=True)

    def test_existing_state_and_identity_are_never_reformatted(self):
        with patch.object(storage.os.path, 'exists', return_value=True), \
                patch.object(storage.subprocess, 'run') as run:
            storage.Storage()._ensure_state_lv()
        run.assert_not_called()


if __name__ == '__main__': unittest.main()
