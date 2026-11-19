"""Real Git checkouts and installed outputs reproduce avoidable cache misses."""
import importlib.util
import json
import sys
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
packages = load('portable_packages', ROOT/'tools/ci/package-cache.py')
kernel = load('portable_kernel', ROOT/'tools/kernel-debug/fingerprint.py')

class CachePortability(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.repo = self.root/'checkout-one'
        self.repo.mkdir(); self.output = self.root/'output'; self.output.mkdir()
        self.files = ['configs/reefy_defconfig','package/example/example.mk',
                      'board/reefy/reefy/kernel-config','tools/ci/build-firmware.sh',
                      'tools/ci/archive-pair.py','tools/ci/bounded_ram.py',
                      'tools/kernel-debug/archive.py','tools/kernel-debug/test_archive.py',
                      'tools/ci/test_synthetic.py']
        for name in self.files:
            p=self.repo/name; p.parent.mkdir(parents=True,exist_ok=True);p.write_text('synthetic original\n')
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','add','.'],cwd=self.repo,check=True)
        subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture'],cwd=self.repo,check=True)
        self.write_config()
    def write_config(self):
        (self.output/'.config').write_text(
            f'BR2_DEFCONFIG="{self.repo}/configs/reefy_defconfig"\n'
            f'BR2_EXTERNAL_REEFY_PATH="{self.repo}"\nBR2_FEATURE=y\n')
    def kernel_key(self):
        previous=Path.cwd()
        try:os.chdir(self.repo);return kernel.fingerprint()
        finally:os.chdir(previous)
    def test_relocated_checkout_preserves_warm_verified_packages(self):
        for name in packages.PACKAGES:
            tree=self.output/'build'/(name+'-1.0');tree.mkdir(parents=True)
            (tree/'.stamp_target_installed').touch()
            if name=='linux-firmware':
                (tree/'.stamp_images_installed').touch();(tree/'br-firmware.tar').write_bytes(b'firmware')
        tools=self.output/'target/usr/bin/pdata_tools';tools.parent.mkdir(parents=True);tools.write_bytes(b'tools')
        packages.refresh(self.repo,self.output,commit=True)
        old=self.repo;self.repo=self.root/'checkout-two';old.rename(self.repo);self.write_config()
        real_run = subprocess.run
        def execute(args, **kwargs):
            if args[0] == 'make': return subprocess.CompletedProcess(args, 0)
            return real_run(args, **kwargs)
        with patch.object(packages.subprocess,'run', side_effect=execute) as run:
            packages.refresh(self.repo,self.output)
            self.assertFalse(any(call.args[0][-1].endswith('-dirclean') for call in run.call_args_list))
        self.assertEqual(tools.read_bytes(),b'tools')
    def test_relocation_does_not_mask_a_real_configuration_change(self):
        first = packages.fingerprint(self.repo, self.output)
        old = self.repo; self.repo = self.root/'checkout-two'; old.rename(self.repo)
        self.write_config()
        self.assertEqual(first, packages.fingerprint(self.repo, self.output))
        with (self.output/'.config').open('a') as f:
            f.write('BR2_TARGET_CFLAGS="-fchanged"\n')
        self.assertNotEqual(first, packages.fingerprint(self.repo, self.output))

    def test_manifest_names_archive_exclusions_and_real_build_inputs(self):
        previous = Path.cwd()
        try:
            os.chdir(self.repo)
            components = {}; kernel.fingerprint(components=components)
        finally:
            os.chdir(previous)
        self.assertIn('file:board/reefy/reefy/kernel-config', components)
        self.assertIn('file:tools/ci/build-firmware.sh', components)
        self.assertNotIn('file:tools/ci/archive-pair.py', components)
        self.assertNotIn('file:tools/kernel-debug/test_archive.py', components)
        self.assertTrue(all(len(value) == 64 for value in components.values()))

    def test_kernel_cli_records_changed_names_without_plaintext_inputs(self):
        compiler = self.output/'host/bin/rustc'; compiler.parent.mkdir(parents=True)
        compiler.write_text('#!/bin/sh\nprintf "synthetic rustc\\n"\n'); compiler.chmod(0o755)
        command = [sys.executable, str(ROOT/'tools/kernel-debug/fingerprint.py'),
                   '--output', str(self.output)]
        first = subprocess.run(command, cwd=self.repo, capture_output=True, text=True, check=True)
        self.assertEqual(len(first.stdout.strip()), 64)
        pending = self.output/'.reefy-kernel-input-components.pending.json'
        committed = self.output/'.reefy-kernel-input-components.json'
        committed.write_bytes(pending.read_bytes())
        original = json.loads(committed.read_text())
        (self.repo/'board/reefy/reefy/kernel-config').write_text('private synthetic compiler option')
        second = subprocess.run(command, cwd=self.repo, capture_output=True, text=True, check=True)
        self.assertNotEqual(first.stdout, second.stdout)
        self.assertIn('file:board/reefy/reefy/kernel-config', second.stderr)
        self.assertNotIn('private synthetic compiler option', pending.read_text()+second.stderr)
        self.assertEqual(json.loads(committed.read_text()), original)

    def test_real_configuration_and_external_inputs_still_invalidate(self):
        first=packages.fingerprint(self.repo,self.output)
        with (self.output/'.config').open('a') as f:f.write('BR2_OTHER_FEATURE=y\n')
        self.assertNotEqual(first,packages.fingerprint(self.repo,self.output));self.write_config()
        with (self.output/'.config').open('a') as f:f.write('BR2_DEFCONFIG="/outside/custom_defconfig"\n')
        self.assertNotEqual(first,packages.fingerprint(self.repo,self.output))
    def test_archive_and_test_edits_preserve_kernel_key(self):
        first=self.kernel_key()
        for name in self.files[4:]:
            p=self.repo/name;p.write_text('synthetic change\n')
            self.assertEqual(first,self.kernel_key(),name)
            p.write_text('synthetic original\n')
        for name in self.files[:4]:
            p=self.repo/name;p.write_text('synthetic build change\n')
            self.assertNotEqual(first,self.kernel_key(),name)
            p.write_text('synthetic original\n')

if __name__=='__main__':unittest.main()
