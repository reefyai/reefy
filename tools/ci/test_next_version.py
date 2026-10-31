import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'next_version', Path(__file__).with_name('next-version.py'))
version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version)


class VersionSequenceTests(unittest.TestCase):
    def test_output_switch_uses_highest_floor_and_mirrors_legacy(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [Path(root) / name / 'counter' for name in ('new', 'old', 'shared')]
            paths[1].parent.mkdir()
            paths[1].write_text('2040.01.01-12\n')
            paths[2].parent.mkdir()
            paths[2].write_text('2040.01.01-18\n')
            self.assertEqual(version.allocate('2040.01.01', paths), '2040.01.01-19')
            self.assertTrue(all(p.read_text().strip() == '2040.01.01-19' for p in paths))
            # An old-branch build advances its legacy counter independently.
            paths[1].write_text('2040.01.01-20\n')
            self.assertEqual(version.allocate('2040.01.01', paths), '2040.01.01-21')

    def test_new_day_starts_at_zero(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'counter'
            path.write_text('2040.01.01-99\n')
            self.assertEqual(version.allocate('2040.01.02', [path]), '2040.01.02-00')

    def test_invalid_or_future_floor_stops_without_overwriting(self):
        for value in ('invalid', '2040.01.03-01'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'counter'
                path.write_text(value)
                with self.assertRaises(ValueError):
                    version.allocate('2040.01.02', [path])
                self.assertEqual(path.read_text(), value)

    def test_sequence_above_two_digits_remains_unique(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'counter'
            path.write_text('2040.01.01-99\n')
            self.assertEqual(version.allocate('2040.01.01', [path]), '2040.01.01-100')


preserve_spec = importlib.util.spec_from_file_location(
    'preserve_version', Path(__file__).with_name('preserve-version.py'))
preserve = importlib.util.module_from_spec(preserve_spec)
preserve_spec.loader.exec_module(preserve)


class PreserveSequenceTests(unittest.TestCase):
    @patch.object(preserve, 'published_floor', return_value='2040.01.01-18')
    def test_new_output_directory_preserves_published_floor(self, published):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'new-build'
            preserve.preserve(output, 'synthetic-token')
            self.assertEqual((output / '.reefy-last-version').read_text(), '2040.01.01-18\n')
            self.assertFalse((output / 'build').exists())

    @patch.object(preserve, 'published_floor')
    def test_existing_floor_is_preserved_without_network(self, published):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root)
            sentinel = output / 'synthetic-local'
            sentinel.write_text('preserve')
            (output / '.reefy-last-version').write_text('2040.01.01-19\n')
            preserve.preserve(output, 'synthetic-token')
            published.assert_not_called()
            self.assertEqual(sentinel.read_text(), 'preserve')
            self.assertEqual((output / '.reefy-last-version').read_text(), '2040.01.01-19\n')


class ConcurrentSequenceTests(unittest.TestCase):
    def test_shared_counter_lock_blocks_another_allocator(self):
        import fcntl
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as root:
            shared = Path(root) / 'shared' / 'counter'
            shared.parent.mkdir()
            shared.write_text('2040.01.01-18\n')
            lock = Path(str(shared) + '.lock').open('a+')
            fcntl.flock(lock, fcntl.LOCK_EX)
            command = [sys.executable, str(Path(version.__file__)), '--date',
                       '2040.01.01', str(Path(root) / 'private' / 'counter'), str(shared)]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.5)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
                stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(stdout.decode().strip(), '2040.01.01-19')

    def test_private_warm_copies_share_unique_versions_and_preserve_data(self):
        import concurrent.futures
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            shared = root / 'shared' / 'counter'
            shared.parent.mkdir()
            shared.write_text('2040.01.01-18\n')
            private = [root / str(i) / 'counter' for i in range(2)]
            for path in private:
                path.parent.mkdir()
                path.write_text('2040.01.01-18\n')
                path.with_name('sentinel').write_text('synthetic-preserved-data')
            def allocate_one(index):
                return subprocess.check_output(
                    [sys.executable, str(Path(version.__file__)), '--date',
                     '2040.01.01', str(private[index % 2]), str(shared)], text=True).strip()
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                values = list(executor.map(allocate_one, range(32)))
            self.assertEqual(len(set(values)), 32)
            self.assertEqual(set(values), {f'2040.01.01-{i:02d}' for i in range(19, 51)})
            self.assertEqual(shared.read_text().strip(), '2040.01.01-50')
            for path in private:
                self.assertEqual(path.with_name('sentinel').read_text(), 'synthetic-preserved-data')
