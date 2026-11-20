"""Integrated upstream Go security updates need no mutable checkout backport."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('prepare_buildroot', Path(__file__).with_name('prepare-buildroot.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = 'bbbdab36565d4941b6b0449a0eedb5aac8a10c2e'


class GoToolchainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.tree = self.repo / 'buildroot'
        subprocess.run(['git', 'init', '-q', str(self.tree)], check=True)
        for name in ('go.mk', 'go.hash'):
            path = self.tree / 'package/go' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(subprocess.check_output(
                ['git', 'show', f'{UPSTREAM}:package/go/{name}'], cwd=ROOT / 'buildroot'))
        legacy = ROOT / 'support/buildroot/0001-go-security-1.27.3.patch'
        if legacy.exists():
            copied = self.repo / 'support/buildroot' / legacy.name
            copied.parent.mkdir(parents=True)
            copied.write_bytes(legacy.read_bytes())
        subprocess.run(['git', 'add', '.'], cwd=self.tree, check=True)
        subprocess.run(['git', '-c', 'user.name=Synthetic Maintainer',
                        '-c', 'user.email=maintainer@example.com', 'commit', '-qm',
                        'Synthetic source snapshot'], cwd=self.tree, check=True)
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.tree, text=True).strip()
        guard = patch.object(module, 'REVISION', revision)
        guard.start()
        self.addCleanup(guard.stop)

    def snapshot(self):
        return {p.name: p.read_bytes() for p in (self.tree / 'package/go').iterdir()}

    def test_integrated_security_release_is_verified_without_mutation_or_patch(self):
        self.assertIn('GO_VERSION = 1.27.4', (self.tree / 'package/go/go.mk').read_text())
        before = self.snapshot()
        module.prepare(self.repo)
        module.prepare(self.repo)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(subprocess.check_output(['git', 'status', '--porcelain'], cwd=self.tree), b'')

    def test_local_compiler_downgrade_is_refused_without_repairing_it(self):
        recipe = self.tree / 'package/go/go.mk'
        recipe.write_text(recipe.read_text().replace('1.27.4', '1.27.3'))
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'tracked changes'):
            module.prepare(self.repo)
        self.assertEqual(before, self.snapshot())

    def test_local_download_hash_changes_are_refused_without_overwrite(self):
        hashes = self.tree / 'package/go/go.hash'
        hashes.write_text(hashes.read_text() + '\n# synthetic unexpected change\n')
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'tracked changes'):
            module.prepare(self.repo)
        self.assertEqual(before, self.snapshot())

    def test_rejects_wrong_submodule_revision(self):
        with patch.object(module.subprocess, 'check_output', return_value='wrong\n'):
            with self.assertRaisesRegex(RuntimeError, 'revision mismatch'):
                module.prepare(self.repo)


if __name__ == '__main__':
    unittest.main()
