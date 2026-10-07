import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('systemd_config', Path(__file__).with_name('systemd-config.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SystemdConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        (self.output / '.config').write_text('BR2_PACKAGE_SYSTEMD_EFI=y\n')
        self.package = self.output / 'build/systemd-258.7'
        (self.package / 'buildroot-build').mkdir(parents=True)
        (self.package / '.stamp_configured').touch()

    def header(self, value):
        (self.package / 'buildroot-build/config.h').write_text(f'#define ENABLE_EFI {value}\n')

    @patch.object(module.subprocess, 'run')
    def test_changed_option_invalidates_old_binary(self, run):
        self.header(0)
        module.check(self.output)
        run.assert_called_once_with(['make', f'O={self.output}', 'systemd-dirclean'], check=True)
        with self.assertRaisesRegex(RuntimeError, 'expected 1'):
            module.check(self.output, verify=True)

    @patch.object(module.subprocess, 'run')
    def test_current_cache_is_preserved(self, run):
        self.header(1)
        module.check(self.output)
        module.check(self.output, verify=True)
        run.assert_not_called()

    @patch.object(module.subprocess, 'run')
    def test_missing_header_invalidates_configured_cache(self, run):
        module.check(self.output)
        run.assert_called_once()

    def test_cold_build_cannot_pass_verification(self):
        (self.package / '.stamp_configured').unlink()
        module.check(self.output)
        with self.assertRaisesRegex(RuntimeError, 'compiled systemd'):
            module.check(self.output, verify=True)
