import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('partition_tools', ROOT / 'tools/ci/partition-tools.py')
module = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(module)


class PartitionToolsTests(unittest.TestCase):
    def test_firmware_packages_the_scoped_refresh_command(self):
        self.assertIn('BR2_PACKAGE_UTIL_LINUX_PARTX=y', (ROOT / 'configs/reefy_defconfig').read_text().splitlines())

    def test_only_stale_warm_util_linux_is_refreshed(self):
        for warm, installed in ((False, False), (True, False), (True, True)):
            with self.subTest(warm=warm, installed=installed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root / '.config').write_text('BR2_PACKAGE_UTIL_LINUX_PARTX=y\n')
                if warm:
                    tree = root/'build/util-linux-synthetic'; tree.mkdir(parents=True)
                    (tree/'.stamp_target_installed').touch()
                if installed:
                    binary = root/'target/usr/sbin/partx'; binary.parent.mkdir(parents=True); binary.touch()
                with patch.object(module.subprocess, 'run') as run:
                    module.refresh(root)
                    self.assertEqual(run.call_count, int(warm and not installed))
                    if run.called: self.assertEqual(run.call_args.args[0][-1], 'util-linux-dirclean')

    def test_unresolved_dependency_stops_before_build_mutation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(module.subprocess, 'run') as run:
            root = Path(directory); (root/'.config').write_text('')
            with self.assertRaises(RuntimeError): module.refresh(root)
            run.assert_not_called()


if __name__ == '__main__': unittest.main()
