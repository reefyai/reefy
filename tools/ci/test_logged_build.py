import os
from pathlib import Path
import subprocess
import tempfile
import unittest

WRAPPER = Path(__file__).with_name('logged-build.sh')


class LoggedBuildTests(unittest.TestCase):
    def run_script(self, content):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'step.sh'
            script.write_text(content)
            env = dict(os.environ, RUNNER_TEMP=directory, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1')
            env['BR_OUTPUT'] = str(root / 'buildroot')
            result = subprocess.run(['bash', str(WRAPPER), str(script)], env=env,
                                    capture_output=True, text=True)
            log = (root / 'reefy-build-logs/123-1/step.sh.log').read_text()
            return result, log

    def test_failure_preserves_exit_status_and_stops(self):
        result, log = self.run_script('echo before; echo error >&2; exit 17; echo unreachable\n')
        self.assertEqual(result.returncode, 17)
        self.assertIn('before', log)
        self.assertIn('error', log)
        self.assertNotIn('unreachable', log)

    def test_pipeline_failure_is_not_hidden(self):
        result, log = self.run_script('false | cat\necho unreachable\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('unreachable', log)

    def test_buildroot_host_tools_precede_system_path(self):
        result, log = self.run_script('case "$PATH" in "$BR_OUTPUT/host/bin:$BR_OUTPUT/host/sbin:"*) echo correct;; *) exit 7;; esac\n')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(log, 'correct\n')

    def test_success(self):
        result, log = self.run_script('echo complete\n')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(log, 'complete\n')


if __name__ == '__main__':
    unittest.main()
