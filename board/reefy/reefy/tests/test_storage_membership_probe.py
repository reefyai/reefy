"""A slow PV membership read must not be mistaken for empty storage."""
import contextlib
import io
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy import storage


class MembershipProbeTests(unittest.TestCase):
    def exercise(self, duration=0, membership='reefy', returncode=0):
        instance = storage.Storage()
        clock, commands = [0.0], []
        output = io.StringIO()

        def run(command, **kwargs):
            commands.append((command, kwargs))
            if command[0] == 'pvs':
                budget = kwargs['timeout']
                clock[0] += min(duration, budget)
                if duration > budget:
                    raise subprocess.TimeoutExpired(command, budget)
                return SimpleNamespace(returncode=returncode,
                                       stdout=membership + '\n', stderr='synthetic probe error')
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        error, result = None, None
        with patch.object(storage.subprocess, 'run', side_effect=run), \
                patch.object(storage.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(storage.os.path, 'exists',
                             side_effect=lambda path: path == '/dev/reefy/reefy_default'), \
                patch.object(instance, '_require_common_mapper_sector_size'), \
                patch.object(instance, '_ensure_state_lv') as state, \
                contextlib.redirect_stdout(output):
            try:
                result = instance._ensure_lvm_stack(['/dev/mapper/synthetic-owned'])
            except (subprocess.TimeoutExpired, RuntimeError) as caught:
                error = caught
            state.assert_not_called()
        # Existing data must never be recreated, even after a delayed or failed
        # membership read. Activation is the only allowed non-probe command.
        self.assertFalse(any(command[0] in (
            'pvcreate', 'vgcreate', 'vgextend', 'lvcreate', 'mkfs.xfs', 'mkfs.ext4')
            for command, _ in commands))
        self.assertEqual(sum(command[0] == 'pvs' for command, _ in commands), 1)
        if error:
            self.assertEqual([command[0] for command, _ in commands], ['pvs'])
        return result, error, output.getvalue()

    def test_fast_owned_pv_preserves_existing_volume(self):
        result, error, output = self.exercise()
        self.assertIsNone(error)
        self.assertEqual(result, 'existing')
        self.assertNotIn('Storage command pvs', output)

    def test_fifteen_second_owned_pv_is_not_recreated(self):
        result, error, output = self.exercise(duration=15)
        self.assertIsNone(error)
        self.assertEqual(result, 'existing')
        self.assertIn('Storage command pvs elapsed=15.000s budget=30s', output)

    def test_permanent_stall_refuses_at_thirty_seconds_without_replay(self):
        result, error, output = self.exercise(duration=120)
        self.assertIsNone(result)
        self.assertIsInstance(error, subprocess.TimeoutExpired)
        self.assertEqual(error.timeout, 30)
        self.assertIn('Storage command pvs elapsed=30.000s budget=30s', output)

    def test_failed_membership_is_not_treated_as_unowned(self):
        _, error, _ = self.exercise(membership='', returncode=5)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn('Cannot inspect PV', str(error))

    def test_foreign_membership_still_refuses(self):
        _, error, _ = self.exercise(membership='synthetic-foreign')
        self.assertIsInstance(error, RuntimeError)
        self.assertIn('already belongs to VG synthetic-foreign', str(error))


if __name__ == '__main__':
    unittest.main()
