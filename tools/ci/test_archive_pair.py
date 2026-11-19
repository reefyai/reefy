"""Exercise real child overlap, failure cleanup and independent data."""
import importlib.util
from pathlib import Path
import os
import json
import shutil
import subprocess
import sys
import tempfile
import unittest


def helper():
    spec = importlib.util.spec_from_file_location(
        'archive_pair', Path(__file__).with_name('archive-pair.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArchivePairTests(unittest.TestCase):
    def test_children_overlap_and_preserve_independent_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sentinel = root / 'sentinel'
            sentinel.write_text('preserved')
            code = '''import sys,time
from pathlib import Path
root=Path(sys.argv[1]);name=sys.argv[2];other=sys.argv[3]
(root/name).write_text('started')
deadline=time.monotonic()+3
while not (root/other).exists():
 if time.monotonic()>deadline:raise RuntimeError('peer did not overlap')
 time.sleep(.01)
(root/(name+'.archive')).write_text('verified-'+name)
'''
            commands = {name: [sys.executable, '-c', code, str(root), name, other]
                        for name, other in [('kernel', 'userspace'), ('userspace', 'kernel')]}
            result = helper().run_pair(commands, root/'logs', sorted(os.sched_getaffinity(0))[:2])
            self.assertEqual(result['archives_verified'], ['kernel', 'userspace'])
            for name in commands:
                self.assertEqual((root/(name+'.archive')).read_text(), 'verified-'+name)
            self.assertEqual(sentinel.read_text(), 'preserved')

    def test_failure_terminates_and_reaps_sibling(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pid = root/'pid'
            slow = [sys.executable, '-c',
                    'import os,time,sys;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)', str(pid)]
            fail = [sys.executable, '-c',
                    'import time,sys;from pathlib import Path\np=Path(sys.argv[1]);end=time.monotonic()+3\nwhile not p.exists() and time.monotonic()<end:time.sleep(.01)\nraise SystemExit(7)', str(pid)]
            with self.assertRaisesRegex(RuntimeError, 'userspace'):
                helper().run_pair({'kernel': slow, 'userspace': fail}, root/'logs',
                                  sorted(os.sched_getaffinity(0))[:2])
            self.assertTrue(pid.exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid.read_text()), 0)
            self.assertTrue((root/'logs/userspace.log').exists())

    @unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0,
                         'requires Linux private mount capability')
    def test_entrypoint_stages_both_archives_only_in_bounded_ram(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ci = root/'tools/ci'; ci.mkdir(parents=True)
            for name in ('archive-pair.py', 'bounded_ram.py'):
                shutil.copyfile(Path(__file__).with_name(name), ci/name)
            code = """import json,os,tempfile
from pathlib import Path
name=Path(__file__).parent.name
with tempfile.TemporaryDirectory() as temporary:
 root=Path(temporary);(root/'stage').write_bytes(b'synthetic stage')
 stats=os.statvfs(root)
 Path(os.environ['BR_OUTPUT'],name+'.json').write_text(json.dumps(dict(
  temporary=str(root),limit=stats.f_blocks*stats.f_frsize)))
Path(os.environ['BR_OUTPUT'],name+'.archive').write_bytes(b'verified synthetic archive')
"""
            for name in ('kernel-debug', 'userspace-debug'):
                directory = root/'tools'/name; directory.mkdir()
                (directory/'archive.py').write_text(code)
            out = root/'output'; out.mkdir()
            sentinel = out/'sentinel'; sentinel.write_bytes(b'preserved')
            subprocess.run([sys.executable, str(ci/'archive-pair.py')], cwd=root,
                           env=dict(os.environ, BR_OUTPUT=str(out)), check=True,
                           capture_output=True, timeout=40)
            for name in ('kernel-debug', 'userspace-debug'):
                receipt = json.loads((out/(name+'.json')).read_text())
                self.assertEqual(receipt['limit'], 32*1024**3)
                self.assertTrue(receipt['temporary'].startswith('/dev/shm/reefy-debug-'))
                self.assertFalse(Path(receipt['temporary']).exists())
                self.assertEqual((out/(name+'.archive')).read_bytes(), b'verified synthetic archive')
            self.assertEqual(sentinel.read_bytes(), b'preserved')

    def test_workflow_requires_pair_before_both_uploads(self):
        workflow = Path(__file__).resolve().parents[2]/'.github/workflows/firmware-build.yml'
        source = workflow.read_text()
        pair = source.index('run: python3 tools/ci/archive-pair.py')
        self.assertLess(pair, source.index('- name: Upload kernel debug archive'))
        self.assertLess(pair, source.index('- name: Upload system userspace debug archive'))
        self.assertIn('name: reefy-kernel-debug', source)
        self.assertIn('name: reefy-userspace-debug', source)
        self.assertIn('name: reefy-userspace-debug-index', source)
        self.assertNotIn('run: python3 tools/userspace-debug/archive.py archive --output', source)


if __name__ == '__main__':
    unittest.main()
