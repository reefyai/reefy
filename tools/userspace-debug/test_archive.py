import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import platform

spec = importlib.util.spec_from_file_location('userspace_archive', Path(__file__).with_name('archive.py'))
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


@unittest.skipUnless(platform.system() == 'Linux' and shutil.which('gcc') and shutil.which('strip'), 'requires Linux ELF tools')
class NativeSymbolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'target/usr/bin/synthetic'
        self.binary.parent.mkdir(parents=True)
        source = self.root / 'synthetic.c'
        source.write_text('int main(void) { return 42; }\n')
        subprocess.run(['gcc', '-g2', '-O1', '-Wl,--build-id=sha1', str(source), '-o', str(self.binary)], check=True, capture_output=True)
        if archive.elf(self.binary) is None:
            self.skipTest('compiler does not produce Linux amd64 ELF')

    def test_captures_before_strip_and_reuses_exact_warm_cache(self):
        original = archive.elf(self.binary)
        self.assertTrue(original['dwarf'])
        archive.capture(self.root)
        subprocess.run(['strip', '--strip-unneeded', str(self.binary)], check=True)
        stripped = archive.elf(self.binary)
        self.assertFalse(stripped['dwarf'])
        self.assertEqual(stripped['code_sha256'], original['code_sha256'])
        archive.capture(self.root)
        saved = self.root / 'reefy-userspace-symbols' / archive.key(stripped)
        self.assertTrue(archive.elf(saved)['dwarf'])
        self.assertEqual(archive.elf(saved)['build_id'], stripped['build_id'])

    def test_missing_build_identity_is_rejected_even_with_dwarf(self):
        source = self.root / 'no_identity.c'
        source.write_text('int main(void) { return 42; }\n')
        subprocess.run(['gcc', '-g2', '-Wl,--build-id=none', str(source),
                        '-o', str(self.binary)], check=True, capture_output=True)
        with self.assertRaisesRegex(RuntimeError, 'lacks GNU/Go build identity'):
            archive.capture(self.root)

    def test_missing_dwarf_without_a_matching_cache_is_a_failure(self):
        subprocess.run(['strip', '--strip-unneeded', str(self.binary)], check=True)
        with self.assertRaisesRegex(RuntimeError, 'no DWARF'):
            archive.capture(self.root)

    def test_matching_build_id_alone_does_not_accept_wrong_code(self):
        archive.capture(self.root)
        info = archive.elf(self.binary)
        saved = self.root / 'reefy-userspace-symbols' / archive.key(info)
        # Same section layout/build note, different machine code in the cache.
        data = saved.read_bytes()
        program = self.binary.read_bytes()
        marker = bytes.fromhex('b82a000000')
        position = data.find(marker)
        self.assertGreater(position, 0)
        changed = bytearray(data)
        changed[position + 1] = 43
        saved.write_bytes(changed)
        subprocess.run(['strip', '--strip-unneeded', str(self.binary)], check=True)
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            archive.capture(self.root)


if __name__ == '__main__':
    unittest.main()
