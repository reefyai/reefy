"""Security pins and warm-install trust removal with synthetic local state."""
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(os.environ.get('REEFY_TLS_SOURCE_ROOT', Path(__file__).resolve().parents[2]))


class TrustPackageTests(unittest.TestCase):
    def test_global_patch_directories_survive_a_clean_checkout(self):
        config = (ROOT / 'configs/reefy_defconfig').read_text()
        value = re.search(r'^BR2_GLOBAL_PATCH_DIR="(.*)"$', config, re.M)[1]
        for entry in value.split():
            path = Path(entry.replace('$(BR2_EXTERNAL_REEFY_PATH)', str(ROOT)))
            self.assertTrue(any(p.is_file() for p in path.rglob('*')), str(path))

    def test_security_backport_is_present(self):
        patch = ROOT / 'board/reefy/reefy/patches/ca-certificates/0002-remove-eMudhra-parents.patch'
        additions = [line for line in patch.read_text().splitlines()
                     if line.startswith('+"')]
        self.assertEqual(additions, ['+"emSign ECC Root CA - G3"', '+"Comodo AAA Services root"'])

    def test_warm_install_removes_retired_roots_and_preserves_local_and_mqtt_ca(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target, host = root / 'target', root / 'host'
            for tree in (target, host):
                for name in ('usr/share/ca-certificates/mozilla/retired-root.crt',
                             'usr/share/ca-certificates/local/synthetic-local.crt',
                             'mnt/reefy/mqtt/ca.crt'):
                    path = tree / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('synthetic certificate sentinel')
            makefile = root / 'Makefile'
            makefile.write_text(
                f'TARGET_DIR = {target}\nHOST_DIR = {host}\n'
                f'include $(wildcard {ROOT}/board/reefy/reefy/package-overrides/*.mk)\n'
                'all:\n'
                '\t$(foreach hook,$(CA_CERTIFICATES_PRE_INSTALL_TARGET_HOOKS),$(call $(hook)))\n'
                '\t$(foreach hook,$(HOST_CA_CERTIFICATES_PRE_INSTALL_HOOKS),$(call $(hook)))\n')
            subprocess.run(['make', '-f', str(makefile)], check=True, capture_output=True)
            for tree in (target, host):
                self.assertFalse((tree / 'usr/share/ca-certificates/mozilla/retired-root.crt').exists())
                for name in ('usr/share/ca-certificates/local/synthetic-local.crt',
                             'mnt/reefy/mqtt/ca.crt'):
                    self.assertEqual((tree / name).read_text(), 'synthetic certificate sentinel')


if __name__ == '__main__':
    unittest.main()
