import importlib.util
from pathlib import Path
import unittest
import shutil
import subprocess
import tempfile
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('archive', Path(__file__).with_name('archive.py'))
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


class ArtifactChecks(unittest.TestCase):
    @unittest.skipUnless(shutil.which('readelf') and shutil.which('gcc') and shutil.which('strip'), 'requires ELF toolchain')
    def test_real_elf_identity_survives_stripping_but_dwarf_does_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'fixture.c'
            binary = Path(temporary) / 'fixture'
            source.write_text('int main(void) { return 0; }\n')
            subprocess.run(['gcc', '-g', '-Wl,--build-id', str(source), '-o', str(binary)], check=True)
            identity = archive.require_debug(binary, 'readelf', btf=False)
            subprocess.run(['strip', '--strip-debug', str(binary)], check=True)
            self.assertEqual(archive.elf_info(binary, 'readelf')[0], identity)
            with self.assertRaisesRegex(RuntimeError, 'missing required'):
                archive.require_debug(binary, 'readelf', btf=False)

    def test_stripped_original_rejected(self):
        with patch.object(archive, 'run', side_effect=['Build ID: abc123', ' .BTF PROGBITS ']):
            with self.assertRaisesRegex(RuntimeError, 'missing required'):
                archive.require_debug(Path('module.ko'), 'readelf')

    def test_missing_btf_rejected(self):
        with patch.object(archive, 'run', side_effect=['Build ID: abc123', ' .debug_info PROGBITS ']):
            with self.assertRaisesRegex(RuntimeError, 'missing required'):
                archive.require_debug(Path('module.ko'), 'readelf')

    def test_missing_identity_rejected(self):
        with patch.object(archive, 'run', return_value=''):
            with self.assertRaisesRegex(RuntimeError, 'missing GNU build ID'):
                archive.elf_info(Path('module.ko'), 'readelf')

    def test_complete_original_accepted(self):
        with patch.object(archive, 'run', side_effect=['Build ID: abc123', ' .debug_info PROGBITS\n .BTF PROGBITS ']):
            self.assertEqual(archive.require_debug(Path('module.ko'), 'readelf'), 'abc123')


class ParallelArchive(unittest.TestCase):
    def test_parallel_results_preserve_input_order(self):
        self.assertEqual(archive.parallel_map(lambda n: n * n, [4, 1, 3]), [16, 1, 9])

    def test_worker_failure_is_not_suppressed(self):
        def verify(value):
            if value == 2:
                raise RuntimeError('module verification failed')
            return value
        with self.assertRaisesRegex(RuntimeError, 'module verification failed'):
            archive.parallel_map(verify, [1, 2, 3])

    @unittest.skipUnless(shutil.which('pigz'), 'requires pigz')
    def test_parallel_gzip_preserves_payload_and_propagates_failure(self):
        import tarfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / 'bundle'
            stage.mkdir()
            payload = b'debug fixture' * 10000
            (stage / 'vmlinux').write_bytes(payload)
            destination = root / 'debug.tar.gz'
            archive.compress_bundle(stage, destination)
            with tarfile.open(destination, 'r:gz') as bundle:
                self.assertEqual(bundle.extractfile('./vmlinux').read(), payload)
            with self.assertRaises(subprocess.CalledProcessError):
                archive.compress_bundle(root / 'missing', destination)


if __name__ == '__main__':
    unittest.main()
