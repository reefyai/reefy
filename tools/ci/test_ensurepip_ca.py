"""Bootstrap wheel CA edits preserve code, verify hashes, and repeat safely."""
import base64
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('ensurepip_ca', Path(__file__).with_name('patch-ensurepip-ca.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@unittest.skipUnless(importlib.util.find_spec('wheel'), 'requires standard wheel tooling')
class BootstrapCATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = self.root / 'source'
        self.bundle = source / module.BUNDLE
        self.bundle.parent.mkdir(parents=True)
        self.blocked = ssl.DER_cert_to_PEM_cert(b'synthetic blocked certificate').encode()
        self.kept = ssl.DER_cert_to_PEM_cert(b'synthetic retained certificate').encode()
        self.bundle.write_bytes(self.blocked + self.kept)
        info = source / 'pip-1.0.dist-info'
        info.mkdir()
        (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: pip\nVersion: 1.0\n')
        (info / 'WHEEL').write_text('Wheel-Version: 1.0\nGenerator: synthetic-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        (source / 'synthetic-local.txt').write_text('preserve synthetic payload')
        subprocess.run([sys.executable, '-m', 'wheel', 'pack', str(source), '-d', str(self.root)], check=True, capture_output=True)
        self.wheel = self.root / 'pip-1.0-py3-none-any.whl'
        self.patch = self.root / 'synthetic.patch'
        self.patch.write_text(''.join(difflib.unified_diff(
            (self.blocked + self.kept).decode().splitlines(True), self.kept.decode().splitlines(True),
            fromfile='a/src/' + module.BUNDLE, tofile='b/src/' + module.BUNDLE)))
        self.fingerprint = hashlib.sha256(b'synthetic blocked certificate').hexdigest()

    def test_repack_preserves_payload_and_roots_and_is_idempotent(self):
        result = module.repack(self.wheel, [self.patch], self.fingerprint)
        self.assertTrue(result['changed'])
        with zipfile.ZipFile(self.wheel) as archive:
            self.assertEqual(archive.read(module.BUNDLE), self.kept)
            self.assertEqual(archive.read('synthetic-local.txt'), b'preserve synthetic payload')
            record = archive.read('pip-1.0.dist-info/RECORD').decode()
            digest = base64.urlsafe_b64encode(hashlib.sha256(self.kept).digest()).rstrip(b'=').decode()
            self.assertIn(module.BUNDLE + ',sha256=' + digest + ',' + str(len(self.kept)), record)
        before = self.wheel.read_bytes()
        self.assertFalse(module.repack(self.wheel, [self.patch], self.fingerprint)['changed'])
        self.assertEqual(self.wheel.read_bytes(), before)

    def test_bad_record_refuses_without_overwriting_wheel(self):
        with zipfile.ZipFile(self.wheel) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        files['synthetic-local.txt'] = b'synthetic corrupted payload'
        with zipfile.ZipFile(self.wheel, 'w') as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        before = self.wheel.read_bytes()
        with self.assertRaises(subprocess.CalledProcessError):
            module.repack(self.wheel, [self.patch], self.fingerprint)
        self.assertEqual(self.wheel.read_bytes(), before)

    def test_fixed_upstream_wheel_accepts_retired_backports_without_changing_data(self):
        destination = self.root / 'target'
        wheel = destination / 'usr/lib/python3.14/ensurepip/_bundled' / self.wheel.name
        wheel.parent.mkdir(parents=True)
        wheel.write_bytes(self.wheel.read_bytes())
        sentinel = destination / 'mnt/reefy/mqtt/ca.crt'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b'synthetic private CA sentinel')
        report = self.root / 'report.json'
        before = wheel.read_bytes()
        subprocess.run([sys.executable, str(Path(module.__file__)), '--root', str(destination),
                        '--patch-dir', str(self.root / 'retired-patches'),
                        '--report', str(report)], check=True, capture_output=True)
        result = json.loads(report.read_text())
        self.assertEqual(result['patch_sha256'], {})
        self.assertFalse(next(iter(result['wheels'].values()))['changed'])
        self.assertEqual(wheel.read_bytes(), before)
        self.assertEqual(sentinel.read_bytes(), b'synthetic private CA sentinel')

    def test_unsafe_wheel_without_backports_fails_before_repacking(self):
        before = self.wheel.read_bytes()
        with patch.object(module.subprocess, 'run', side_effect=AssertionError(
                'Unreviewed wheel must not reach subprocess tools')) as run:
            with self.assertRaisesRegex(RuntimeError, 'no reviewed backports'):
                module.repack(self.wheel, [], self.fingerprint)
        run.assert_not_called()
        self.assertEqual(self.wheel.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
