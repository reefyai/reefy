"""Pinned Go must not execute a different cached distribution."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class GoToolchainTests(unittest.TestCase):
    def test_versioned_root_uses_its_own_compiler_and_preserves_legacy_cache(self):
        override = Path(os.environ.get('REEFY_GO_OVERRIDES',
            Path(__file__).resolve().parents[2] / 'board/reefy/reefy/package-overrides.mk'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = root / 'host'
            legacy = host / 'lib/go/src'
            legacy.mkdir(parents=True)
            sentinel = legacy / 'synthetic-stale-file.go'
            sentinel.write_text('synthetic legacy source')
            for relative, body in [
                ('bin/go', '#!/bin/sh\nexit 99\n'),
                ('lib/go-1.27.2/bin/go', '#!/bin/sh\nprintf "go1.27.2\\n"\n'),
            ]:
                path = host / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                path.chmod(0o755)
            makefile = root / 'Makefile'
            makefile.write_text(
                f'HOST_DIR = {host}\ninclude {override}\n'
                'GO_VERSION = 1.26.6\n'
                'HOST_GO_ROOT = $(HOST_DIR)/lib/go\n'
                'GO_BIN = $(HOST_DIR)/bin/go\n'
                'check:\n\t@$(GO_BIN) version\n'
                '\t@test "$(HOST_GO_ROOT)" = "$(HOST_DIR)/lib/go-1.27.2"\n')
            result = subprocess.run(['make', '-f', str(makefile), 'check'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'go1.27.2')
            self.assertEqual(sentinel.read_text(), 'synthetic legacy source')


if __name__ == '__main__':
    unittest.main()
