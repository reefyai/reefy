import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import platform
import json
import tarfile
from unittest.mock import patch

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

    @unittest.skipUnless(shutil.which('gdb') and shutil.which('pigz'), 'requires native debugger/compressor')
    def test_archived_symbols_resolve_a_real_core_to_source_line(self):
        package = self.root / 'build/synthetic-crash'
        package.mkdir(parents=True)
        (package / '.stamp_target_installed').touch()
        source = package / 'crash.c'
        source.write_text('#include <stdlib.h>\n__attribute__((noinline)) void crash_me(void) { abort(); }\nint main(void) { crash_me(); return 0; }\n')
        subprocess.run(['gcc', '-g2', '-O1', '-Wl,--build-id=sha1', str(source), '-o', str(self.binary)], check=True)
        core = self.root / 'synthetic.core'
        generated = subprocess.run(['gdb', '-nx', '-batch', '-ex', 'set auto-load off', '-ex', 'run',
                        '-ex', 'generate-core-file ' + str(core), '--args', str(self.binary)],
                       capture_output=True, text=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        self.assertTrue(core.is_file())
        archive.capture(self.root)
        subprocess.run(['strip', '--strip-unneeded', str(self.binary)], check=True)
        (self.root / '.config').write_text('BR2_ENABLE_DEBUG=y\n')
        release = self.root / 'target/usr/lib/os-release'
        release.parent.mkdir(parents=True)
        release.write_text('IMAGE_VERSION=2099.01.01-01\nREEFY_BUILD_ID=' + 'a' * 64 + '\n')
        bundle = self.root / 'debug.tar.gz'
        with patch.object(archive.subprocess, 'check_output', return_value='synthetic-source\n'):
            archive.archive(self.root, bundle)
        unpacked = self.root / 'unpacked'
        unpacked.mkdir()
        with tarfile.open(bundle) as tar:
            tar.extractall(unpacked, filter='data')
        record = json.loads((unpacked / 'metadata.json').read_text())['elfs'][0]
        self.assertTrue(record['dwarf'])
        self.assertFalse(record['shipped_dwarf'])
        result = subprocess.run(['gdb', '-nx', '-batch', '-ex', 'set auto-load off',
            '-ex', 'set debug-file-directory ' + str(unpacked / 'debug'),
            '-ex', 'set substitute-path ' + str(self.root) + ' ' + str(unpacked / 'sources'),
            '-ex', 'bt', str(unpacked / 'unstripped/usr/bin/synthetic'), str(core)],
            check=True, capture_output=True, text=True, timeout=30)
        self.assertIn('crash_me', result.stdout)
        self.assertRegex(result.stdout, r'crash\.c:[1-9][0-9]*')
        self.assertTrue((unpacked / 'sources/build/synthetic-crash/crash.c').is_file())


    @unittest.skipUnless(shutil.which('gdb') and shutil.which('pigz'), 'requires native debugger/compressor')
    def test_minimal_core_omits_heap_and_resolves_faulting_source(self):
        package = self.root / 'build/synthetic-minimal'
        package.mkdir(parents=True)
        (package / '.stamp_target_installed').touch()
        marker = b'REEFY_SYNTHETIC_PRIVATE_HEAP_CONTENT'
        source = package / 'minimal.c'
        source.write_text(
            '#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n'
            'char *private_heap;\n'
            '__attribute__((noinline)) void crash_me(void) { __asm__ volatile ("ud2"); }\n'
            'int main(void) { private_heap = malloc(8 * 1024 * 1024); '
            'memset(private_heap, 65, 8 * 1024 * 1024); '
            'strcpy(private_heap, "REEFY_SYNTHETIC_PRIVATE_HEAP_CONTENT"); '
            'FILE *f = fopen("/proc/self/coredump_filter", "w"); '
            'if (!f) return 2; fputs("0x10", f); fclose(f); '
            'crash_me(); return private_heap[0]; }\n')
        subprocess.run(['gcc', '-g2', '-O1', '-Wl,--build-id=sha1',
                        str(source), '-o', str(self.binary)], check=True)
        core = self.root / 'minimal.core'
        generated = subprocess.run([
            'gdb', '-nx', '-batch', '-ex', 'set auto-load off',
            '-ex', 'set use-coredump-filter on', '-ex', 'run',
            '-ex', 'x/s private_heap',
            '-ex', 'generate-core-file ' + str(core), '--args', str(self.binary)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        self.assertIn(marker.decode(), generated.stdout)
        self.assertTrue(core.is_file())
        self.assertLess(core.stat().st_size, 1024 * 1024)
        self.assertNotIn(marker, core.read_bytes())
        archive.capture(self.root)
        subprocess.run(['strip', '--strip-unneeded', str(self.binary)], check=True)
        info = archive.elf(self.binary)
        original = self.root / 'reefy-userspace-symbols' / archive.key(info)
        result = subprocess.run([
            'gdb', '-nx', '-batch', '-ex', 'set auto-load off',
            '-ex', 'frame 0', '-ex', 'info line *$pc', str(original), str(core)],
            check=True, capture_output=True, text=True, timeout=30)
        self.assertIn('crash_me', result.stdout)
        self.assertRegex(result.stdout, r'minimal\.c:[1-9][0-9]*')


if __name__ == '__main__':
    unittest.main()
