"""Compiler changes invalidate consumers; successful verification marks reuse."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('go_cache', Path(__file__).with_name('go-cache.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class GoCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.output = self.repo / 'output'
        self.output.mkdir()
        pin = self.repo / 'buildroot/package/go/go.mk'
        pin.parent.mkdir(parents=True)
        pin.write_text('GO_VERSION = 1.27.3\n')
        self.marker = self.output / '.reefy-go-version'

    @patch.object(module.subprocess, 'run')
    @patch.object(module.subprocess, 'check_output', return_value='docker-engine tailscale-reefy docker-engine')
    def test_cold_changed_and_interrupted_build_refresh_all_consumers(self, query, run):
        for before in (None, '1.27.2', None):
            if before:
                self.marker.write_text(before)
            run.reset_mock()
            module.refresh(self.repo, self.output)
            self.assertEqual([c.args[0][-1] for c in run.call_args_list],
                             ['docker-engine-dirclean', 'host-go-bin-dirclean', 'host-go-src-dirclean', 'tailscale-reefy-dirclean'])
            self.assertFalse(self.marker.exists())

    @patch.object(module.subprocess, 'run')
    @patch.object(module.subprocess, 'check_output', return_value='docker-engine')
    def test_standard_root_is_replaced_and_local_state_preserved(self, query, run):
        stale = self.output / 'host/lib/go/src/retired.go'
        stale.parent.mkdir(parents=True)
        stale.write_text('synthetic stale standard library')
        local = self.output / 'host/lib/synthetic-local'
        local.write_text('preserve')
        module.refresh(self.repo, self.output)
        self.assertFalse(stale.exists())
        self.assertEqual(local.read_text(), 'preserve')
        self.assertFalse(self.marker.exists())

    @patch.object(module.subprocess, 'check_output')
    def test_same_compiler_reuses_packages(self, query):
        self.marker.write_text('1.27.3\n')
        compiler = self.output / 'host/lib/go/bin/go'
        compiler.parent.mkdir(parents=True)
        compiler.touch()
        module.refresh(self.repo, self.output)
        query.assert_not_called()

    @patch.object(module.subprocess, 'check_output')
    def test_commit_refuses_stale_target_binary(self, query):
        self.marker.write_text('1.27.3\n')
        query.side_effect = ['go version go1.27.3 linux/amd64', '/target/usr/bin/dockerd: go1.26.6\n']
        with self.assertRaisesRegex(RuntimeError, 'stale Go'):
            module.refresh(self.repo, self.output, commit=True)
        self.assertFalse(self.marker.exists())

    @patch.object(module.subprocess, 'check_output')
    def test_commit_records_all_verified_binaries(self, query):
        query.side_effect = ['go version go1.27.3 linux/amd64', '/target/usr/bin/dockerd: go1.27.3\n/target/usr/bin/tailscale: go1.27.3\n']
        module.refresh(self.repo, self.output, commit=True)
        self.assertEqual(self.marker.read_text(), '1.27.3\n')
        self.assertTrue((self.output / 'reefy-go-binaries.json').is_file())

    @patch.object(module.subprocess, 'check_output', return_value='unsafe;name')
    @patch.object(module.subprocess, 'run')
    def test_refuses_invalid_buildroot_output_before_mutation(self, run, query):
        with self.assertRaisesRegex(RuntimeError, 'invalid'):
            module.refresh(self.repo, self.output)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
