import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('version_floor', Path(__file__).parents[1] / 'ci/preserve-version.py')
version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version)


class FirmwareSequenceTests(unittest.TestCase):
    def test_preserves_larger_legacy_counter_before_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build').mkdir()
            (root / '.reefy-last-version').write_text('2099.01.01-09')
            (root / 'build/.reefy-last-version').write_text('2099.01.01-100')
            version.preserve(root)
            self.assertEqual((root / '.reefy-last-version').read_text().strip(), '2099.01.01-100')

    def test_missing_local_state_uses_published_floor(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(version, 'published_floor', return_value='2099.01.01-12'):
            root = Path(directory)
            version.preserve(root, 'synthetic-token')
            self.assertEqual((root / '.reefy-last-version').read_text().strip(), '2099.01.01-12')

    def test_unavailable_published_floor_does_not_allocate_a_duplicate(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(version, 'published_floor', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                version.preserve(Path(directory), 'synthetic-token')

    def test_invalid_local_counter_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.reefy-last-version').write_text('invalid')
            with self.assertRaises(RuntimeError):
                version.preserve(root)
