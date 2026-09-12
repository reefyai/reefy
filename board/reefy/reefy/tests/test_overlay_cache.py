"""Build-cache cleanup must not leak development units or prune active ones."""

from pathlib import Path
import subprocess
import tempfile
import unittest


class OverlayCacheTests(unittest.TestCase):
    def test_prunes_only_obsolete_known_files(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/prune_overlay_cache.sh'
        with tempfile.TemporaryDirectory() as directory:
            overlay = Path(directory) / 'overlay'
            target = Path(directory) / 'target'
            for root in (overlay, target):
                (root / 'usr/lib/reefy').mkdir(parents=True)
            obsolete = 'usr/lib/systemd/system/reefy-storage-watchdog.service'
            dropin = 'etc/systemd/system/docker.service.d/storage-guard.conf'
            retained = 'usr/lib/systemd/system/reefy-storage-guard.service'
            current = 'usr/lib/systemd/system/reefy-storage.service'
            package = 'usr/lib/systemd/system/reefy-cmds.service'
            for name in (obsolete, dropin, retained, current, package):
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic unit\n')
            source = overlay / retained
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('current unit\n')
            for _ in range(2):
                subprocess.run(['bash', str(script), str(overlay), str(target)], check=True)
                self.assertFalse((target / obsolete).exists())
                self.assertFalse((target / dropin).exists())
                for name in (retained, current, package):
                    self.assertEqual((target / name).read_text(), 'synthetic unit\n')


if __name__ == '__main__':
    unittest.main()
