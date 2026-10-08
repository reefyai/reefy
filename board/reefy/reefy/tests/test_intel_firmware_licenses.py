"""Execute provider notice staging against upstream's old and new layouts."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / 'board/reefy/reefy/post_build.sh'


class IntelFirmwareLicenseTests(unittest.TestCase):
    def _stage(self, source, target):
        script = SCRIPT.read_text()
        start = script.index('for license in LICENSE.i915 LICENSE.xe; do\n')
        end = script.index('\ndone\n', start) + len('\ndone\n')
        return subprocess.run(
            ['bash', '-eu', '-c', script[start:end]],
            env={**os.environ, 'LINUX_FIRMWARE_BUILD': str(source),
                 'INTEL_FIRMWARE': str(target)},
            capture_output=True, text=True)

    def test_old_and_new_layout_preserve_notice_contents_and_names(self):
        for layout in ('', 'LICENSES'):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as root:
                source, target = Path(root) / 'source', Path(root) / 'target'
                notices = source / layout
                notices.mkdir(parents=True)
                destination = target / 'usr/share/licenses/intel-provider'
                destination.mkdir(parents=True)
                for name in ('LICENSE.i915', 'LICENSE.xe'):
                    (notices / name).write_text('synthetic notice: ' + name)
                result = self._stage(source, target)
                self.assertEqual(result.returncode, 0, result.stderr)
                for name in ('LICENSE.i915', 'LICENSE.xe'):
                    self.assertEqual((destination / name).read_bytes(),
                                     (notices / name).read_bytes())

    def test_new_layout_takes_precedence_over_legacy_copy(self):
        with tempfile.TemporaryDirectory() as root:
            source, target = Path(root) / 'source', Path(root) / 'target'
            (source / 'LICENSES').mkdir(parents=True)
            destination = target / 'usr/share/licenses/intel-provider'
            destination.mkdir(parents=True)
            for name in ('LICENSE.i915', 'LICENSE.xe'):
                (source / name).write_text('synthetic old notice')
                (source / 'LICENSES' / name).write_text('synthetic current notice')
            result = self._stage(source, target)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((destination / 'LICENSE.i915').read_text(),
                             'synthetic current notice')

    def test_missing_required_notice_stops_packaging(self):
        with tempfile.TemporaryDirectory() as root:
            source, target = Path(root) / 'source', Path(root) / 'target'
            source.mkdir()
            (target / 'usr/share/licenses/intel-provider').mkdir(parents=True)
            result = self._stage(source, target)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('missing required Intel firmware license', result.stderr)
