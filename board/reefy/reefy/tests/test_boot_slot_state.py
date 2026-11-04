"""Mounted/default identity is read-only and independent of reused boot numbers."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

LIBRARY = Path(__file__).parents[1] / 'rootfs-overlay/usr/lib/reefy/boot-slot-state.sh'
A = '12345678-abcd-abcd-abcd-123456789abc'
B = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


class BootSlotStateTests(unittest.TestCase):
    def state(self, **changes):
        value = {'source': '/dev/sda1', 'guid': A, 'efi': (
            'BootCurrent: 0001\nBootOrder: 0000,0001\n'
            f'Boot0000* reefy-a HD(1,GPT,{A},0x800,0x200000)\n'
            f'Boot0001* reefy-b HD(2,GPT,{B},0x200800,0x200000)\n')}
        value.update(changes)
        return value

    def exercise(self, value):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = root / 'shim'
            shim.write_text(f'#!{sys.executable}\n' + '''import json,os,pathlib,sys
data=json.loads(os.environ['SYNTHETIC_BOOT_STATE'])
name=pathlib.Path(sys.argv[0]).name
if data.get('fail_command')==name: sys.exit(1)
if name=='findmnt': print(data['source'])
elif name=='blkid': print(data['guid'])
elif name=='efibootmgr':
 if sys.argv[1:]!=['-v']:
  pathlib.Path(os.environ['SYNTHETIC_MUTATION']).touch();sys.exit(99)
 print(data['efi'])
''')
            shim.chmod(0o755)
            for name in ('findmnt', 'blkid', 'efibootmgr'):
                (root / name).symlink_to(shim)
            result = subprocess.run(['sh', '-c', '. ' + shlex.quote(str(LIBRARY)) +
                                     '\nmounted_slot_is_default'], capture_output=True, text=True,
                env={**os.environ, 'PATH': directory + ':' + os.environ['PATH'],
                     'SYNTHETIC_BOOT_STATE': json.dumps(value),
                     'SYNTHETIC_MUTATION': str(root / 'mutation')}, timeout=10)
            self.assertFalse((root / 'mutation').exists())
            return result.returncode

    def test_already_default_ignores_stale_current_number(self):
        self.assertEqual(self.exercise(self.state()), 0)

    def test_pending_detected_even_when_current_number_matches_default(self):
        value = self.state()
        value['efi'] = value['efi'].replace('BootOrder: 0000,0001', 'BootOrder: 0001,0000')
        self.assertEqual(self.exercise(value), 1)

    def test_guid_matching_is_case_insensitive(self):
        self.assertEqual(self.exercise(self.state(guid=A.upper())), 0)

    def test_unknown_metadata_is_never_an_already_confirmed_slot(self):
        values = [self.state(source=''), self.state(guid=''), self.state(efi=''),
                  self.state(efi='BootOrder: 1234\n'),
                  self.state(efi='BootOrder: invalid\n'),
                  self.state(efi='BootOrder: 0000\nBoot0000* Network VenHw(synthetic)\n')]
        duplicate = self.state()
        duplicate['efi'] += f'Boot0000* duplicate HD(1,GPT,{A},0x800,0x200000)\n'
        values.append(duplicate)
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(self.exercise(value), 2)

    def test_lookup_failure_is_unknown_without_writes(self):
        for command in ('findmnt', 'blkid', 'efibootmgr'):
            with self.subTest(command=command):
                self.assertEqual(self.exercise(self.state(fail_command=command)), 2)


if __name__ == '__main__':
    unittest.main()
