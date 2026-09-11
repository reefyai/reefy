from contextlib import nullcontext
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401
from reefy.storage_download import download


class DownloadTests(unittest.TestCase):
    def test_failed_copy_never_creates_a_completed_model(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'model'

            def curl(args, **kwargs):
                Path(args[args.index('-o') + 1]).write_bytes(b'partial')
                raise subprocess.CalledProcessError(23, args)

            with patch('reefy.storage_download.Registry', return_value=Mock(data={'active': False})), \
                    patch('reefy.storage_download.subprocess.run', side_effect=curl):
                with self.assertRaises(subprocess.CalledProcessError):
                    download(directory, str(destination), 'https://download.invalid/model')
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_final_redirect_size_is_admitted_before_atomic_install(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'model'
            registry = Mock(data={'active': True, 'projects': {'destination': {
                'path': directory, 'complete': True, 'storage_class': 'bulk'}}})
            events = []

            def curl(args, **kwargs):
                if '-fsSLI' in args:
                    return types.SimpleNamespace(stdout='HTTP/1.1 302 Redirect\nContent-Length: 1\n\nHTTP/2 200\nContent-Length: 8193\n\n')
                events.append('download')
                self.assertFalse(destination.exists())
                Path(args[args.index('-o') + 1]).write_bytes(b'x' * 8193)

            def admit(kind, budget, **kwargs):
                events.append('admission')
                self.assertEqual(kwargs, {'storage_class': 'bulk', 'target': 'destination'})
                self.assertEqual(budget, 64 * 1024**2 + 12288)
                return nullcontext()

            with patch('reefy.storage_download.Registry', return_value=registry), \
                    patch('reefy.storage_download.reservation', side_effect=admit), \
                    patch('reefy.storage_download.subprocess.run', side_effect=curl):
                download(directory, str(destination), 'https://download.invalid/model')
            self.assertEqual(events, ['admission', 'download'])
            self.assertEqual(destination.read_bytes(), b'x' * 8193)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(Path(directory).iterdir()), [destination])
