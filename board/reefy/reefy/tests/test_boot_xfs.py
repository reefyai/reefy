"""Execute the real shell recovery function against fault-injected commands."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / 'rootfs-overlay/usr/bin/boot-reefy-storage.sh'
COMPLETE = ('Phase 7 - verify link counts...\n'
            'No modify flag set, skipping filesystem flush and exiting.\n')


class PrimaryXfsTests(unittest.TestCase):
    def execute(self, responses):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = root / 'shim'
            shim.write_text(f'#!{sys.executable}\n' + '''import json, os, pathlib, sys
root = pathlib.Path(os.environ['FAKE_ROOT'])
name = pathlib.Path(sys.argv[0]).name
key = ' '.join([name] + (sys.argv[1:2] if name == 'xfs_repair' and sys.argv[1].startswith('-') else []))
plan = json.loads((root / 'plan.json').read_text())
with (root / 'calls.jsonl').open('a') as f:
    f.write(json.dumps([name] + sys.argv[1:]) + '\\n')
queue = plan.get(key, [])
if not queue:
    print('unexpected command: ' + key, file=sys.stderr)
    sys.exit(99)
code, out = queue.pop(0)
(root / 'plan.json').write_text(json.dumps(plan))
print(out, file=sys.stderr if name == 'mount' else sys.stdout)
sys.exit(code)
''')
            shim.chmod(0o755)
            for name in ('mount', 'umount', 'xfs_repair', 'findmnt', 'mountpoint', 'blkid'):
                (root / name).symlink_to(shim)
            defaults = {'findmnt': [[1, '']], 'blkid': [[0, 'xfs']],
                        'mountpoint': [[1, '']]}
            defaults.update(responses)
            (root / 'plan.json').write_text(json.dumps(defaults))
            source = SCRIPT.read_text()
            function = source[source.index('mount_primary_xfs() {'):source.index('\n# Repair an inactive')]
            function = function.replace('/run/', str(root) + '/')
            command = ('STORAGE_VG=reefy\nSTORAGE_LV=reefy_default\n' + function
                       + '\nmount_primary_xfs /dev/reefy/reefy_default /mnt/test noatime\n')
            result = subprocess.run(['sh', '-c', command], text=True,
                                    capture_output=True, env={**os.environ,
                                    'FAKE_ROOT': directory, 'PATH': directory + ':' + os.environ['PATH']})
            calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
            return result, calls

    def test_late_corruption_checked_repaired_and_verified(self):
        result, calls = self.execute({'mount': [[0, ''], [0, '']], 'umount': [[0, '']],
                                     'xfs_repair -n': [[1, COMPLETE], [0, COMPLETE]],
                                     'xfs_repair': [[0, 'repaired']]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([c[:2] for c in calls if c[0] == 'xfs_repair'],
                         [['xfs_repair', '-n'], ['xfs_repair', '/dev/reefy/reefy_default'],
                          ['xfs_repair', '-n']])

    def test_clean_filesystem_never_repaired(self):
        result, calls = self.execute({'mount': [[0, ''], [0, '']], 'umount': [[0, '']],
                                     'xfs_repair -n': [[0, COMPLETE]]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([c for c in calls if c[0] == 'xfs_repair']), 1)

    def test_mount_operational_error_does_not_repair(self):
        result, calls = self.execute({'mount': [[32, 'wrong fs type, bad option']]})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[0] == 'xfs_repair' for c in calls))

    def test_failed_unmount_never_checks_or_repairs(self):
        result, calls = self.execute({'mount': [[0, '']], 'umount': [[1, 'busy']]})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[0] == 'xfs_repair' for c in calls))

    def test_incomplete_scan_does_not_repair(self):
        for code, text in [(1, 'Input/output error'), (2, 'dirty log'), (137, 'killed'),
                           (1, 'Input/output error\n' + COMPLETE),
                           (1, 'short read\n' + COMPLETE)]:
            with self.subTest(code=code):
                result, calls = self.execute({'mount': [[0, '']], 'umount': [[0, '']],
                                             'xfs_repair -n': [[code, text]]})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c[0] == 'xfs_repair' and c[1] != '-n' for c in calls))

    def test_failed_verification_does_not_expose_filesystem(self):
        result, calls = self.execute({'mount': [[0, '']], 'umount': [[0, '']],
                                     'xfs_repair -n': [[1, COMPLETE], [1, COMPLETE]],
                                     'xfs_repair': [[0, '']]})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len([c for c in calls if c[0] == 'mount']), 1)

    def test_mounted_elsewhere_is_refused(self):
        result, calls = self.execute({'findmnt': [[0, '/somewhere/else']]})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)

    def test_clean_replay_never_permits_log_reset(self):
        result, calls = self.execute({'mount': [[0, '']], 'umount': [[0, '']],
                                     'xfs_repair -n': [[1, COMPLETE]],
                                     'xfs_repair': [[2, 'dirty log']]})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(['xfs_repair', '-L', '/dev/reefy/reefy_default'], calls)

    def test_failed_replay_retains_guarded_log_reset_and_verification(self):
        result, calls = self.execute({'mount': [[32, 'Structure needs cleaning'], [0, '']],
                                     'xfs_repair': [[2, 'dirty log']],
                                     'xfs_repair -L': [[0, '']],
                                     'xfs_repair -n': [[0, COMPLETE]]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(['xfs_repair', '-L', '/dev/reefy/reefy_default'], calls)
        self.assertEqual(calls[-2], ['xfs_repair', '-n', '/dev/reefy/reefy_default'])


if __name__ == '__main__':
    unittest.main()
