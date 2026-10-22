"""Real Go regression for mgmt's exporter compatibility, enabled after Go install."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get('REEFY_TEST_GO'), 'requires the installed firmware Go compiler')
class GoLoaderTests(unittest.TestCase):
    def test_old_exporter_fails_and_selected_upstream_exporter_loads_real_types(self):
        compiler = str(Path(os.environ['REEFY_TEST_GO']).resolve())
        recipe = (ROOT / 'package/mgmt/mgmt.mk').read_text()
        selected = re.search(r'^MGMT_X_TOOLS_VERSION = (v[0-9.]+)$', recipe, re.M)[1]
        fixture = ROOT / 'tools/ci/fixtures/go-loader.go'
        report = {
            'source': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'compiler': subprocess.check_output([compiler, 'version'], text=True).strip(),
            'probe_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
            'upstream_fix': 'https://github.com/golang/tools/commit/89ed5c340cb6d4a9437f801cfc718ac5980c938d',
            'cases': [],
        }
        env = dict(os.environ, GOTOOLCHAIN='local', GOFLAGS='', GOWORK='off',
                   GOPROXY='https://proxy.golang.org,direct', GOSUMDB='sum.golang.org')
        # GOROOT may refer to a different host toolchain in the calling shell.
        env.pop('GOROOT', None)
        try:
            with tempfile.TemporaryDirectory() as folder:
                for name, version in [('baseline', 'v0.41.0'), ('fixed', selected)]:
                    tree = Path(folder) / name
                    tree.mkdir()
                    shutil.copyfile(fixture, tree / 'main.go')
                    (tree / 'go.mod').write_text(
                        'module example.invalid/synthetic-loader\n\ngo 1.26.0\n\n'
                        f'require golang.org/x/tools {version}\n')
                    setup = subprocess.run([compiler, 'mod', 'tidy'], cwd=tree, env=env,
                                           text=True, capture_output=True, timeout=300)
                    self.assertEqual(setup.returncode, 0, setup.stderr)
                    result = subprocess.run([compiler, 'run', '.', 'html'], cwd=tree, env=env,
                                            text=True, capture_output=True, timeout=300)
                    case = {'name': name, 'x_tools': version, 'exit_code': result.returncode,
                            'stdout': result.stdout, 'stderr': result.stderr,
                            'go_mod': (tree / 'go.mod').read_text(),
                            'go_sum': (tree / 'go.sum').read_text()}
                    report['cases'].append(case)
                    if name == 'baseline':
                        self.assertNotEqual(result.returncode, 0)
                        self.assertRegex(result.stderr, 'without types|export data version')
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn('package type loading passed', result.stderr)
                        negative = subprocess.run([compiler, 'run', '.', 'synthetic_missing_package'],
                            cwd=tree, env=env, text=True, capture_output=True, timeout=300)
                        case['missing_package_exit_code'] = negative.returncode
                        case['missing_package_stderr'] = negative.stderr
                        self.assertNotEqual(negative.returncode, 0)
                        self.assertIn('package type loading failed', negative.stderr)
        finally:
            if destination := os.environ.get('REEFY_TEST_GO_RESULTS'):
                Path(destination).write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
