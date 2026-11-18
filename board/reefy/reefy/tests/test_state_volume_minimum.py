"""Fresh small-media state volumes must be formatable without touching existing ones."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy import storage


class StateVolumeMinimumTests(unittest.TestCase):
    def provision(self, vg_bytes):
        calls = []
        state_bytes = None

        def run(command, **kwargs):
            nonlocal state_bytes
            calls.append(command)
            if command[0] == 'vgs':
                return SimpleNamespace(returncode=0, stdout=str(vg_bytes), stderr='')
            if command[0] == 'lvcreate':
                state_bytes = int(command[command.index('-L') + 1].removesuffix('M')) * 1024**2
            if command[0] == 'mkfs.xfs' and state_bytes <= 300 * 1024**2:
                return SimpleNamespace(returncode=1, stdout='',
                                       stderr='Filesystem must be larger than 300MB.')
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        with patch.object(storage.os.path, 'exists', return_value=False), \
                patch.object(storage.subprocess, 'run', side_effect=run):
            storage.Storage()._ensure_state_lv()
        self.assertEqual([cmd[0] for cmd in calls], ['vgs', 'lvcreate', 'mkfs.xfs'])
        return state_bytes

    def test_one_gib_disk_can_format_state_and_leave_room_for_data(self):
        # LUKS and VG overhead make the usable VG smaller than the 1 GiB disk.
        vg_bytes = 1000 * 1024**2
        size = self.provision(vg_bytes)
        self.assertEqual(size, 512 * 1024**2)
        self.assertGreater(vg_bytes - size, 300 * 1024**2)

    def test_entire_subminimum_range_is_formatable(self):
        for vg_gib in (1, 2, 3, 4, 5):
            with self.subTest(vg_gib=vg_gib):
                self.assertGreater(self.provision(vg_gib * 1024**3), 300 * 1024**2)

    def test_proportional_size_and_four_gib_cap_are_preserved(self):
        for vg_gib, expected_mib in ((8, 819), (32, 3276), (80, 4096)):
            with self.subTest(vg_gib=vg_gib):
                self.assertEqual(self.provision(vg_gib * 1024**3), expected_mib * 1024**2)

    def test_existing_volume_is_never_resized_or_reformatted(self):
        with patch.object(storage.os.path, 'exists', return_value=True), \
                patch.object(storage.subprocess, 'run') as run:
            storage.Storage()._ensure_state_lv()
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
