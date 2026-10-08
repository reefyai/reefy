import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('fingerprint', Path(__file__).with_name('fingerprint.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CacheInputs(unittest.TestCase):
    def test_runtime_changes_preserve_key_build_changes_invalidate(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                subprocess.run(['git', 'init', '-q'], check=True)
                files = ['board/reefy/reefy/rootfs-overlay/usr/lib/service.py',
                         'board/reefy/reefy/tests/test_service.py',
                         'board/reefy/reefy/kernel-config',
                         'board/reefy/reefy/new-patches/fix.patch',
                         'board/reefy/reefy/pre_build.sh',
                         'configs/reefy_defconfig', 'package/driver/driver.mk',
                         'external.mk', '.github/workflows/firmware-build.yml',
                         'tools/ci/build-amd.sh']
                for name in files:
                    path = Path(name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('original\n')
                subprocess.run(['git', 'add', '.'], check=True)
                subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                'commit', '-qm', 'fixture'], check=True)
                baseline = module.fingerprint()
                for index, name in enumerate(files):
                    Path(name).write_text('changed\n')
                    if index < 2:
                        self.assertEqual(module.fingerprint(), baseline, name)
                    else:
                        self.assertNotEqual(module.fingerprint(), baseline, name)
                    Path(name).write_text('original\n')
                compiler = Path(directory) / 'output/host/bin/rustc'
                compiler.parent.mkdir(parents=True)
                compiler.write_text('#!/bin/sh\nprintf "rustc synthetic-one\\n"\n')
                compiler.chmod(0o755)
                with_toolchain = module.fingerprint(compiler.parents[2])
                self.assertEqual(module.fingerprint(compiler.parents[2]), with_toolchain)
                compiler.write_text('#!/bin/sh\nprintf "rustc synthetic-two\\n"\n')
                self.assertNotEqual(module.fingerprint(compiler.parents[2]), with_toolchain)
                compiler.unlink()
                with self.assertRaises(FileNotFoundError):
                    module.fingerprint(compiler.parents[2])
            finally:
                os.chdir(previous)
