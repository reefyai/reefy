"""ZIP migration verifies contents before permitting a device reflash."""
from pathlib import Path
import tempfile
import unittest
import zipfile

import _bootstrap
from reefy.image_download import normalize, zip_url


class DownloadTests(unittest.TestCase):
    def test_format_selection_keeps_authorization_query(self):
        self.assertEqual(zip_url('https://images.invalid/download?token=synthetic&format=raw'),
                         'https://images.invalid/download?token=synthetic&format=zip')

    def test_zip_and_legacy_raw_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.raw'
            data = b'MZsynthetic-firmware' * 10000
            for zipped in (False, True):
                if zipped:
                    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
                        archive.writestr('synthetic.raw', data)
                else:
                    path.write_bytes(data)
                normalize(path, len(data))
                self.assertEqual(path.read_bytes(), data)

    def test_unsafe_members_bad_size_and_truncation_preserve_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.raw'
            for name, expected, truncate in [('../escape.raw', 4, False),
                                              ('image.raw', 8, False),
                                              ('image.raw', 4, True)]:
                with self.subTest(name=name, expected=expected, truncate=truncate):
                    with zipfile.ZipFile(path, 'w') as archive:
                        archive.writestr(name, b'data')
                    if truncate:
                        path.write_bytes(path.read_bytes()[:-10])
                    before = path.read_bytes()
                    with self.assertRaises((ValueError, zipfile.BadZipFile)):
                        normalize(path, expected)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertFalse(path.with_suffix('.extract').exists())
