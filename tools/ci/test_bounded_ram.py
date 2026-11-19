"""Bounded scratch rejects memory pressure and cleans up on every exit."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import signal
import unittest
from unittest.mock import patch

MODULE = Path(__file__).with_name('bounded_ram.py')
if not MODULE.exists():
    MODULE = Path(__file__).resolve().parents[1] / 'e2e/lib/bounded_ram.py'
spec = importlib.util.spec_from_file_location('bounded_ram', MODULE)
ram = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ram)


class BoundedRamTests(unittest.TestCase):
    def test_admission_requires_host_and_cgroup_reserve(self):
        with patch.object(ram, 'memory_headroom', return_value=(100, 100)):
            ram.require_capacity(60, host_reserve=30, group_reserve=20)
        for capacity in [(80, 100), (100, 70)]:
            with patch.object(ram, 'memory_headroom', return_value=capacity):
                with self.assertRaisesRegex(RuntimeError, 'RAM staging unavailable'):
                    ram.require_capacity(60, host_reserve=30, group_reserve=20)

    def test_nested_cgroup_limits_include_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); child = root/'job'; child.mkdir()
            for folder, limit, current in [(root, '1000', '800'), (child, 'max', '10')]:
                (folder/'memory.max').write_text(limit)
                (folder/'memory.high').write_text('max')
                (folder/'memory.current').write_text(current)
            self.assertEqual(ram.cgroup_headroom(child, root), 200)

    @unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0,
                         'requires Linux private mount capability')
    def test_native_limit_cleanup_failure_and_disk_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); sentinel = root/'sentinel'; sentinel.write_text('keep')
            output = root/'output'; receipt = root/'scratch'
            code = """import errno,os,tempfile,sys
from pathlib import Path
Path(sys.argv[2]).write_text(tempfile.gettempdir())
with tempfile.TemporaryDirectory() as folder:
 p=Path(folder)/'large'
 try:
  with p.open('wb', buffering=0) as f:
   for _ in range(32): f.write(b'x'*1048576)
 except OSError as e:
  assert e.errno==errno.ENOSPC
 else: raise AssertionError('scratch escaped its cap')
Path(sys.argv[1]).write_text('verified')
"""
            ram.run_in_ram([sys.executable, '-c', code, str(output), str(receipt)], 8*1024**2)
            self.assertEqual(output.read_text(), 'verified')
            self.assertFalse(Path(receipt.read_text()).exists())
            fail = "import tempfile,sys;from pathlib import Path;Path(sys.argv[1]).write_text(tempfile.gettempdir());raise SystemExit(7)"
            with self.assertRaises(subprocess.CalledProcessError):
                ram.run_in_ram([sys.executable, '-c', fail, str(receipt)], 8*1024**2)
            self.assertFalse(Path(receipt.read_text()).exists())
            self.assertEqual(sentinel.read_text(), 'keep')


    @unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0,
                         'requires Linux private mount capability')
    def test_cancellation_drains_child_and_removes_private_scratch(self):
        with tempfile.TemporaryDirectory() as folder:
            receipt = Path(folder)/'receipt'
            child_code = "import os,tempfile,time,sys;from pathlib import Path;Path(sys.argv[1]).write_text(tempfile.gettempdir()+'\\n'+str(os.getpid()));time.sleep(60)"
            launcher = ("import sys;sys.path.insert(0,sys.argv[1]);from bounded_ram import run_in_ram;"
                        "run_in_ram([sys.executable,'-c',sys.argv[2],sys.argv[3]],8*1024**2)")
            child = subprocess.Popen([sys.executable, '-c', launcher, str(MODULE.parent),
                                      child_code, str(receipt)], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic()+10
                while not receipt.exists() and child.poll() is None and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertTrue(receipt.exists(), child.communicate(timeout=1) if child.poll() is not None else 'no receipt')
                scratch, pid = receipt.read_text().splitlines()
                child.send_signal(signal.SIGTERM)
                child.communicate(timeout=25)
                self.assertNotEqual(child.returncode, 0)
                self.assertFalse(Path(scratch).exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid), 0)
            finally:
                if child.poll() is None:
                    child.kill(); child.communicate()

if __name__ == '__main__':
    unittest.main()
