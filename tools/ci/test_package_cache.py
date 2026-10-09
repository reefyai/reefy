import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('package_cache', Path(__file__).with_name('package-cache.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PackageRefreshTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'repo'
        self.repo.mkdir()
        self.output = Path(tmp.name) / 'output'
        self.output.mkdir()
        (self.output / '.config').write_text('CONFIG_INITIAL=y\n')
        for name in module.PACKAGES:
            tree = self.output / 'build' / (name + '-1.0')
            tree.mkdir(parents=True)
            (tree / '.stamp_target_installed').touch()
        firmware = self.output / 'build/linux-firmware-1.0'
        (firmware / '.stamp_images_installed').touch()
        (firmware / 'br-firmware.tar').write_bytes(b'synthetic firmware')
        tools = self.output / 'target/usr/bin/pdata_tools'
        tools.parent.mkdir(parents=True)
        tools.write_bytes(b'synthetic thin tools')
        self.marker = self.output / '.reefy-package-refresh.json'
        self.key = 'inputs-one'
        key = patch.object(module, 'fingerprint', side_effect=lambda *args: self.key)
        key.start()
        self.addCleanup(key.stop)

    def commit(self):
        module.refresh(self.repo, self.output, commit=True)

    @patch.object(module.subprocess, 'run')
    def test_cold_then_warm_then_interrupted_build(self, run):
        module.refresh(self.repo, self.output)
        self.assertEqual(run.call_count, 2)
        self.commit()
        run.reset_mock()
        module.refresh(self.repo, self.output)
        run.assert_not_called()
        self.assertFalse(self.marker.exists())
        # No successful commit after interruption means the next run refreshes.
        module.refresh(self.repo, self.output)
        self.assertEqual(run.call_count, 2)

    @patch.object(module.subprocess, 'run')
    def test_changed_inputs_refresh_both_packages(self, run):
        self.commit()
        self.key = 'inputs-two'
        module.refresh(self.repo, self.output)
        self.assertEqual(run.call_count, 2)

    @patch.object(module.subprocess, 'run')
    def test_damaged_tools_refresh_only_tools(self, run):
        self.commit()
        (self.output / 'target/usr/bin/pdata_tools').write_bytes(b'corrupt')
        module.refresh(self.repo, self.output)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], 'thin-provisioning-tools-dirclean')

    @patch.object(module.subprocess, 'run')
    def test_missing_stamp_refreshes_firmware(self, run):
        self.commit()
        (self.output / 'build/linux-firmware-1.0/.stamp_images_installed').unlink()
        module.refresh(self.repo, self.output)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], 'linux-firmware-dirclean')
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            self.commit()
        self.assertFalse(self.marker.exists())

    @patch.object(module.subprocess, 'run')
    def test_invalid_marker_is_a_cache_miss(self, run):
        for content in ('invalid', '[]', '{"outputs":[]}'):
            with self.subTest(content=content):
                self.marker.write_text(content)
                run.reset_mock()
                module.refresh(self.repo, self.output)
                self.assertEqual(run.call_count, 2)


class PackageFingerprintTests(unittest.TestCase):
    def test_configuration_recipes_and_buildroot_change_key_runtime_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            output = repo / 'output'
            output.mkdir()
            (output / '.config').write_text('CONFIG_INITIAL=y\n')
            names = ['package/thin-provisioning-tools/recipe.mk',
                     'board/reefy/reefy/rootfs-overlay/app.py',
                     'board/reefy/reefy/post_build.sh']
            for name in names:
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('original')
            tree = b'160000 commit synthetic-buildroot\tbuildroot\n'
            def git(args, **kwargs):
                return tree if 'ls-tree' in args else b'\0'.join(n.encode() for n in names)
            with patch.object(module.subprocess, 'check_output', side_effect=git):
                baseline = module.fingerprint(repo, output)
                for name in names:
                    (repo / name).write_text('changed')
                    changed = module.fingerprint(repo, output)
                    if 'rootfs-overlay' in name:
                        self.assertEqual(changed, baseline)
                    else:
                        self.assertNotEqual(changed, baseline)
                    (repo / name).write_text('original')
                (output / '.config').write_text('CONFIG_NEW_OPTION=y\n')
                self.assertNotEqual(module.fingerprint(repo, output), baseline)
                (output / '.config').write_text('CONFIG_INITIAL=y\n')
                tree = b'160000 commit changed-buildroot\tbuildroot\n'
                self.assertNotEqual(module.fingerprint(repo, output), baseline)


class ConfigurationSnapshotTests(unittest.TestCase):
    def test_generated_comments_and_order_do_not_change_resolved_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / '.config'
            config.write_text('# generated header\nBR2_ALPHA=y\nBR2_BETA="value"\n')
            first = module.configuration(root)
            config.write_text('BR2_BETA="value"\n# regenerated header\nBR2_ALPHA=y\n')
            self.assertEqual(first, module.configuration(root))
            config.write_text('BR2_BETA="changed"\nBR2_ALPHA=y\n')
            self.assertNotEqual(first, module.configuration(root))

    def test_invalid_configuration_is_not_a_cache_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.config').write_text('malformed input\n')
            with self.assertRaises(ValueError):
                module.configuration(root)
