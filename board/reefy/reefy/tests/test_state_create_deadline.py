"""State formatting requires a real creator exit within the bounded allowance."""
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy import storage


class StateCreateDeadlineTests(unittest.TestCase):
    def provision(self, duration, calls):
        completed = False

        def run(command, **kwargs):
            nonlocal completed
            calls.append((command, kwargs))
            if command[0] == 'vgs':
                return SimpleNamespace(returncode=0, stdout=str(32 * 1024**3), stderr='')
            if command[0] == 'lvcreate':
                self.assertNotIn('--noudevsync', command)
                self.assertNotIn('--nolocking', command)
                if duration > kwargs['timeout']:
                    raise subprocess.TimeoutExpired(command, kwargs['timeout'])
                completed = True
            if command[0] == 'lvs':
                return SimpleNamespace(returncode=0,
                    stdout='{"report":[{"lv":[{"lv_name":"reefy_state"}]}]}', stderr='')
            if command[0] == 'mkfs.xfs':
                self.assertTrue(completed, 'Metadata existence cannot authorize formatting')
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        with patch.object(storage.os.path, 'exists', return_value=False), \
                patch.object(storage.subprocess, 'run', side_effect=run):
            storage.Storage()._ensure_state_lv()

    def test_slow_creator_can_finish_before_formatting(self):
        calls = []
        self.provision(30, calls)
        self.assertEqual([cmd[0] for cmd, _ in calls], ['vgs', 'lvcreate', 'mkfs.xfs'])
        create = next(kw for cmd, kw in calls if cmd[0] == 'lvcreate')
        self.assertEqual(create['timeout'], 60)
        self.assertEqual(next(kw for cmd, kw in calls if cmd[0] == 'mkfs.xfs')['timeout'], 120)

    def test_exhausted_creator_preserves_committed_volume_without_formatting(self):
        calls = []
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.provision(61, calls)
        self.assertEqual(caught.exception.timeout, 60)
        self.assertEqual([cmd[0] for cmd, _ in calls], ['vgs', 'lvcreate', 'lvs'])


if __name__ == '__main__':
    unittest.main()
