"""The exact upstream patch applies to stable, repeats safely and rejects drift."""
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
PATCH = ROOT / 'support/buildroot/0001-go-security-1.27.3.patch'


class GoToolchainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.tree)], check=True)
        for name in ('go.mk', 'go.hash'):
            path = self.tree / 'package/go' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(subprocess.check_output(
                ['git', 'show', f'{module.REVISION}:package/go/{name}'], cwd=ROOT / 'buildroot'))

    def test_unmodified_stable_has_old_go_then_verbatim_backport_is_idempotent(self):
        recipe = self.tree / 'package/go/go.mk'
        self.assertIn('GO_VERSION = 1.27.2', recipe.read_text())
        module.apply_backport(self.tree, PATCH)
        self.assertIn('GO_VERSION = 1.27.3', recipe.read_text())
        self.assertNotIn('go1.27.2.', (self.tree / 'package/go/go.hash').read_text())
        before = {p: p.read_bytes() for p in (recipe, self.tree / 'package/go/go.hash')}
        module.apply_backport(self.tree, PATCH)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_conflict_fails_without_partial_changes(self):
        recipe = self.tree / 'package/go/go.mk'
        recipe.write_text(recipe.read_text().replace('1.27.2', '1.25.0'))
        before = (self.tree / 'package/go/go.hash').read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'neither applies'):
            module.apply_backport(self.tree, PATCH)
        self.assertEqual(before, (self.tree / 'package/go/go.hash').read_bytes())

    def test_modified_patch_is_rejected(self):
        patch = self.tree / 'tampered.patch'
        patch.write_bytes(PATCH.read_bytes() + b'\n')
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            module.apply_backport(self.tree, patch)

    def test_rejects_wrong_submodule_revision(self):
        with patch.object(module.subprocess, 'check_output', return_value='wrong\n'):
            with self.assertRaisesRegex(RuntimeError, 'revision mismatch'):
                module.prepare(ROOT)


if __name__ == '__main__':
    unittest.main()
