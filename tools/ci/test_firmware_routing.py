"""Keep firmware prerequisites off the general acceptance-test queue."""
from pathlib import Path
import re
import unittest


WORKFLOW = Path(__file__).resolve().parents[2] / '.github/workflows/firmware-build.yml'


class FirmwareRoutingTests(unittest.TestCase):
    def job(self, name):
        jobs = WORKFLOW.read_text().split('\njobs:\n', 1)[1]
        match = re.search(r'^  ' + re.escape(name) +
                          r':\n(.*?)(?=^  [\w-]+:|\Z)', jobs, re.M | re.S)
        self.assertIsNotNone(match, f'Missing required firmware job: {name}')
        return match.group(1)

    def test_prerequisites_and_compilation_use_reserved_self_hosted_capacity(self):
        for name in ('debug-archive-tests', 'build'):
            with self.subTest(job=name):
                match = re.search(r'^    runs-on: \[([^\]]+)\]', self.job(name), re.M)
                self.assertIsNotNone(match)
                labels = {value.strip() for value in match.group(1).split(',')}
                self.assertTrue({'self-hosted', 'large'} <= labels)
                self.assertNotIn('big-bird', labels)
                if name == 'debug-archive-tests':
                    self.assertTrue({'Linux', 'X64'} <= labels)

    def test_archive_verification_remains_a_required_build_dependency(self):
        self.assertRegex(self.job('build'), r'(?m)^    needs: debug-archive-tests$')
        checks = self.job('debug-archive-tests')
        for suite in ('tools/kernel-debug', 'tools/userspace-debug', 'tools/ci'):
            self.assertIn(f"python3 -m unittest discover -s {suite} -p 'test_*.py'", checks)


if __name__ == '__main__':
    unittest.main()
