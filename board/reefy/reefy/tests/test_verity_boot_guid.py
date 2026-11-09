"""Use standard EFI UKI partition identity, with read-only fallback lookup."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/verity-boot-guid.sh'
VENDOR = '4a67b082-0a4c-41cf-b6c7-440b29bb8c4f'
A = '12345678-abcd-abcd-abcd-123456789abc'
B = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
C = 'abcdefab-1234-5678-1234-abcdefabcdef'


class BootGuidTests(unittest.TestCase):
    def exercise(self, values, efi=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            variables = root/'vars'
            variables.mkdir()
            originals = {}
            for name, value in values.items():
                path = variables/(name+'-'+VENDOR)
                path.write_bytes(b'\x07\0\0\0'+(value+'\0').encode('utf-16-le'))
                originals[path] = path.read_bytes()
            text = efi if efi is not None else (
                'BootCurrent: 0001\nBoot0001* synthetic-boot HD(1,GPT,'+C+',0x800,0x200000)\n')
            (root/'entries').write_text(text)
            shim = root/'efibootmgr'
            shim.write_text('#!/bin/sh\n[ "$*" = -v ] || exit 99\ncat "$GUID_TEST_ROOT/entries"\n')
            shim.chmod(0o755)
            result = subprocess.run(['sh','-c','. '+shlex.quote(str(SCRIPT))+
                '; verity_boot_guid '+shlex.quote(str(variables))], text=True,
                capture_output=True, env={**os.environ,'GUID_TEST_ROOT':str(root),
                    'PATH':str(root)+':'+os.environ['PATH']},timeout=10)
            self.assertTrue(all(path.read_bytes()==value for path,value in originals.items()))
            return result

    def test_stub_identity_precedes_loader_and_reused_current_entry(self):
        result = self.exercise({'StubDevicePartUUID':A,'LoaderDevicePartUUID':B})
        self.assertEqual((result.returncode,result.stdout.strip()),(0,A))

    def test_loader_identity_handles_firmware_whole_disk_entry(self):
        result = self.exercise({'LoaderDevicePartUUID':B},
            'BootCurrent: 0001\nBoot0001* synthetic-fallback PciRoot(0)/Pci(0,0)\n')
        self.assertEqual((result.returncode,result.stdout.strip()),(0,B))

    def test_invalid_stub_falls_back_to_valid_loader(self):
        result = self.exercise({'StubDevicePartUUID':'invalid','LoaderDevicePartUUID':B})
        self.assertEqual((result.returncode,result.stdout.strip()),(0,B))

    def test_guid_case_is_normalized(self):
        result = self.exercise({'StubDevicePartUUID':A.upper()})
        self.assertEqual((result.returncode,result.stdout.strip()),(0,A))

    def test_missing_variables_use_current_gpt_entry(self):
        result = self.exercise({})
        self.assertEqual((result.returncode,result.stdout.strip()),(0,C))

    def test_ambiguous_current_entries_are_refused(self):
        result = self.exercise({},'BootCurrent: 0001\nBoot0001* one HD(1,GPT,'+A+
            ',0x800,0x200000)\nBoot0001* two HD(2,GPT,'+B+',0x200800,0x200000)\n')
        self.assertNotEqual(result.returncode,0)

    def test_invalid_identity_never_selects_an_arbitrary_partition(self):
        for value in ('', 'not-a-guid', 'a'*36, A+'extra', A.replace('1','-',1), '$(exit 0)'):
            with self.subTest(value=value):
                result = self.exercise({'StubDevicePartUUID':value},'BootCurrent: invalid\n')
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(result.stdout,'')


if __name__ == '__main__':
    unittest.main()
