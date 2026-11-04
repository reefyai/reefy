import copy
import hashlib
import importlib.util
import io
import json
import lzma
from pathlib import Path
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'intel_wifi_firmware', ROOT / 'board/reefy/reefy/scripts/intel_wifi_firmware.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class IntelWifiPackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.kept = 'intel/iwlwifi/iwlwifi-fixture-a0-89.ucode'
        self.old = 'intel/iwlwifi/iwlwifi-fixture-a0-77.ucode'
        self.pnvm = 'intel/iwlwifi/iwlwifi-fixture-a0.pnvm'
        self.other = 'unrelated/controller.bin'
        self.data = {self.kept: b'synthetic firmware API 89' * 200,
                     self.old: b'synthetic firmware API 77' * 200,
                     self.pnvm: b'synthetic calibration', self.other: b'preserve unrelated data'}
        self.rules = {'kernel_ref': 'v6.18.60', 'firmware_version': '20260810',
                      'files': {name: module.digest(self.data[name])
                                for name in (self.kept, self.pnvm)},
                      'kernel_sources': {'drivers/net/wireless/intel/iwlwifi/cfg/fixture.c':
                                         module.digest(b'reviewed API limits')}}
        self.archive = self.root / 'br-firmware.tar'
        self.write_archive(self.data)

    def write_archive(self, entries):
        with tarfile.open(self.archive, 'w') as archive:
            for name, value in entries.items():
                item = tarfile.TarInfo(name)
                item.size = len(value)
                archive.addfile(item, io.BytesIO(value))

    def prepare(self):
        module.prepare(self.archive, self.rules, 'v6.18.60', '20260810')

    def stage(self, destination, target):
        root = module.firmware_root(destination, target)
        root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(self.archive) as archive:
            archive.extractall(root, filter='data')
        for name in (self.kept, self.pnvm):
            (root / Path(name).name).symlink_to(name)
        return root

    def test_prune_preserves_family_calibration_and_unrelated_payload(self):
        self.prepare()
        with tarfile.open(self.archive) as archive:
            self.assertEqual(set(archive.getnames()), {self.kept, self.pnvm, self.other})
            for name in archive.getnames():
                self.assertEqual(archive.extractfile(name).read(), self.data[name])

    def test_upgrade_and_changed_families_fail_without_mutating_archive(self):
        original = self.archive.read_bytes()
        for kernel, firmware in [('v6.18.61', '20260810'), ('v6.18.60', 'new-release')]:
            with self.subTest(kernel=kernel, firmware=firmware):
                with self.assertRaisesRegex(RuntimeError, 'pin changed'):
                    module.prepare(self.archive, self.rules, kernel, firmware)
                self.assertEqual(self.archive.read_bytes(), original)
        changed = dict(self.data, **{'intel/iwlwifi/iwlwifi-new-chip-1.ucode': b'new hardware'})
        self.write_archive(changed)
        original = self.archive.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'chip families changed'):
            self.prepare()
        self.assertEqual(self.archive.read_bytes(), original)

    def test_missing_changed_or_new_calibration_fails_before_pruning(self):
        for name, value in [(self.kept, b'corrupt'), (self.pnvm, None),
                            ('intel/iwlwifi/iwlwifi-new-chip.pnvm', b'new calibration')]:
            changed = dict(self.data)
            if value is None:
                del changed[name]
            else:
                changed[name] = value
            self.write_archive(changed)
            original = self.archive.read_bytes()
            with self.subTest(name=name):
                with self.assertRaises(RuntimeError):
                    self.prepare()
                self.assertEqual(self.archive.read_bytes(), original)

    def test_target_and_images_install_crc32_aliases_and_warm_reinstall(self):
        self.prepare()
        for target in (False, True):
            with self.subTest(target=target):
                destination = self.root / str(target)
                root = self.stage(destination, target)
                report = module.install(root, self.rules)
                self.assertEqual(report['files'], 2)
                self.assertEqual(report['families'], 1)
                for name in (self.kept, self.pnvm):
                    value = (root / (name + '.xz')).read_bytes()
                    decoder = lzma.LZMADecompressor()
                    self.assertEqual(decoder.decompress(value), self.data[name])
                    self.assertEqual(decoder.check, lzma.CHECK_CRC32)
                    self.assertEqual((root / (Path(name).name + '.xz')).read_bytes(), value)
                    self.assertFalse((root / name).exists())
                    self.assertFalse((root / Path(name).name).is_symlink())
                # A warm build can contain both old raw and old compressed files.
                stale = root / self.old
                stale.write_bytes(self.data[self.old])
                stale.with_suffix('.ucode.xz').write_bytes(b'stale')
                (root / Path(self.old).name).symlink_to(self.old)
                unrelated = root / 'intel/iwlwifi/iwlwifi-unmanaged-1.ucode'
                unrelated.write_bytes(b'preserve unowned firmware')
                module.clean(root, self.rules)
                self.assertFalse(stale.exists())
                self.assertFalse(stale.with_suffix('.ucode.xz').exists())
                self.assertEqual(unrelated.read_bytes(), b'preserve unowned firmware')
                self.assertEqual((root / self.other).read_bytes(), self.data[self.other])
                self.stage(destination, target)
                self.assertEqual(module.install(root, self.rules), report)

    def test_corrupt_install_refuses_before_writing_compressed_files(self):
        self.prepare()
        root = self.stage(self.root / 'target', True)
        (root / self.pnvm).write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'changed installed'):
            module.install(root, self.rules)
        self.assertTrue((root / self.kept).is_file())
        self.assertFalse((root / (self.kept + '.xz')).exists())

    def test_verify_rejects_unoptimized_baseline_corruption_and_obsolete_files(self):
        root = self.stage(self.root / 'target', True)
        # The same verification fails on the unmodified broad/raw installation.
        with self.assertRaisesRegex(RuntimeError, 'Compressed Intel firmware missing'):
            module.verify(root, self.rules)
        module.clean(root, self.rules)
        self.prepare()
        self.stage(self.root / 'target', True)
        module.install(root, self.rules)
        stale = root / self.old
        stale.write_bytes(b'obsolete')
        with self.assertRaisesRegex(RuntimeError, 'Obsolete/uncompressed'):
            module.verify(root, self.rules)
        stale.unlink()
        (root / (self.kept + '.xz')).write_bytes(lzma.compress(b'wrong firmware'))
        with self.assertRaisesRegex(RuntimeError, 'firmware changed'):
            module.verify(root, self.rules)

    def test_escaping_paths_and_invalid_manifest_are_refused(self):
        destination = self.root / 'target'
        destination.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        (destination / 'lib').symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'escapes'):
            module.firmware_root(destination, True)
        rules = copy.deepcopy(self.rules)
        rules['files']['../../outside'] = 'invalid'
        manifest = self.root / 'policy.json'
        manifest.write_text(json.dumps(rules))
        with self.assertRaisesRegex(ValueError, 'managed firmware path'):
            module.policy(manifest)

    def test_compiled_kernel_requires_reviewed_sources_and_xz_support(self):
        kernel = self.root / 'kernel'
        path = kernel / next(iter(self.rules['kernel_sources']))
        path.parent.mkdir(parents=True)
        path.write_bytes(b'reviewed API limits')
        config = kernel / '.config'
        config.write_text('CONFIG_FW_LOADER=y\nCONFIG_FW_LOADER_COMPRESS=y\nCONFIG_FW_LOADER_COMPRESS_XZ=y\n')
        module.check_kernel(kernel, self.rules)
        config.write_text('CONFIG_FW_LOADER=y\nCONFIG_FW_LOADER_COMPRESS=y\n')
        with self.assertRaisesRegex(RuntimeError, 'CONFIG_FW_LOADER_COMPRESS_XZ'):
            module.check_kernel(kernel, self.rules)
        path.write_bytes(b'changed API limits')
        with self.assertRaisesRegex(RuntimeError, 'driver source changed'):
            module.check_kernel(kernel, self.rules)


if __name__ == '__main__':
    unittest.main()
