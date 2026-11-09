"""Real veritysetup interoperability, geometry boundaries and dependency copying."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


verity = module('create_verity')
copy = module('copy_initramfs_tools')


class GeometryTests(unittest.TestCase):
    def test_tree_level_boundaries(self):
        for blocks, expected in ((128, 1), (129, 3), (16384, 129), (16385, 132)):
            data, hashes, fec, total = verity.layout(blocks * 4096)
            self.assertEqual((data, hashes, fec), (blocks, expected, blocks + expected))
            self.assertGreater(total, fec)

    def test_absolute_target_symlink_never_reads_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'usr/lib').mkdir(parents=True)
            (root / 'lib').symlink_to('/usr/lib')
            (root / 'usr/lib/test.so').write_bytes(b'target library')
            self.assertEqual(copy.resolve(root, '/lib/test.so'), root / 'usr/lib/test.so')

    def test_reject_invalid_size(self):
        for size in (0, 4095, 4 * 1024**3 + 1):
            with self.assertRaises(ValueError):
                verity.layout(size)


@unittest.skipUnless(shutil.which('veritysetup') and shutil.which('mksquashfs'),
                     'requires Linux veritysetup and squashfs-tools')
class FormatTests(unittest.TestCase):
    def test_real_format_reproducibility_and_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'files').mkdir()
            (root / 'files/payload').write_bytes(bytes(range(256)) * 4096)
            source = root / 'root.squashfs'
            subprocess.run(['mksquashfs', str(root / 'files'), str(source),
                            '-noappend', '-processors', '1', '-noI', '-noD', '-noF'],
                           check=True, capture_output=True)
            first, second = root / 'first', root / 'second'
            one, two = root / 'one.conf', root / 'two.conf'
            verity.build(source, first, one, shutil.which('veritysetup'))
            verity.build(source, second, two, shutil.which('veritysetup'))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(one.read_bytes(), two.read_bytes())
            name, data_device, hash_device, root_hash, options = one.read_text().split()
            self.assertEqual((name, data_device, hash_device),
                             ('reefy-root', '/rootfs.squashfs.verity', '/rootfs.squashfs.verity'))
            values = dict(option.split('=', 1) for option in options.split(','))
            cfg = dict(root_hash=root_hash, salt=values['salt'],
                       data_blocks=values['data-blocks'],
                       hash_start=str(int(values['hash-offset']) // 4096))
            command = ['veritysetup', 'verify', str(first), str(first), cfg['root_hash'],
                       '--no-superblock', '--format=1', '--hash=sha256',
                       '--data-block-size=4096', '--hash-block-size=4096',
                       '--data-blocks=' + cfg['data_blocks'],
                       '--hash-offset=' + str(int(cfg['hash_start']) * 4096),
                       '--salt=' + cfg['salt']]
            subprocess.run(command, check=True, capture_output=True)
            with first.open('r+b') as stream:
                stream.seek(8192)
                stream.write(b'attacker-controlled bytes')
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)



class VeritytabTests(unittest.TestCase):
    """The early glue accepts the generated standard row and fails closed."""
    def check_table(self, content, accepted):
        import shlex
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, payload = root/'veritytab', root/'payload'
            payload.write_bytes(b'synthetic protected image')
            config.write_text(content.replace('/rootfs.squashfs.verity',str(payload)))
            script = (SCRIPTS/'verity-init.sh').read_text()
            script = script.replace('conf=/etc/veritytab','conf='+shlex.quote(str(config)))
            script = script.replace('payload=/rootfs.squashfs.verity','payload='+shlex.quote(str(payload)))
            script = script.replace('/usr/lib/systemd/systemd-veritysetup','native_helper')
            script += '\nnative_helper() { echo NATIVE_ACTIVATION >&2; return 1; }\nverity_activate\n'
            result = subprocess.run(['sh','-c',script],capture_output=True,text=True)
            self.assertEqual('NATIVE_ACTIVATION' in result.stderr,accepted,result.stderr)

    @staticmethod
    def row():
        return ('reefy-root /rootfs.squashfs.verity /rootfs.squashfs.verity '+
                'a'*64+' superblock=no,format=1,hash=sha256,data-blocks=4,hash-offset=16384\n')

    def test_accept_standard_row_and_comments(self):
        self.check_table('# Generated boot mapping\n\n'+self.row(),True)

    def test_accept_final_record_without_newline(self):
        self.check_table(self.row().rstrip('\n'),True)

    def test_reject_duplicate_mapping_and_extra_fields(self):
        self.check_table(self.row()+self.row(),False)
        self.check_table(self.row().rstrip('\n')+' unexpected\n',False)

    def test_reject_wrong_mapping_path_and_digest(self):
        for content in (self.row().replace('reefy-root','other-root'),
                        self.row().replace('/rootfs.squashfs.verity','/untrusted/image'),
                        self.row().replace('a'*64,'invalid')):
            with self.subTest(content=content): self.check_table(content,False)

    def test_reject_options_that_bypass_integrity_or_our_recovery_guard(self):
        for option in ('ignore-corruption','ignore-zero-blocks','check-at-most-once',
                       'restart-on-corruption','panic-on-corruption','restart-on-error','panic-on-error'):
            with self.subTest(option=option):
                self.check_table(self.row().rstrip('\n')+','+option+'\n',False)

    def test_reject_empty_configuration(self):
        self.check_table('# no root mapping\n',False)


class ConfirmationTests(unittest.TestCase):
    """Confirmed-slot recovery history clears only after fresh health checks."""

    def exercise(self, default=True, corrupt_after_wait=False, control_failed=False, monitor_failed=False, monitor_fails_after_wait=False, monitor_restarted=False):
        import os
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'runtime/bin').mkdir(parents=True)
            (root / 'ticks').write_text('0')
            (root / 'slot.sh').write_text('mounted_slot_is_default() { return ' +
                                          ('0' if default else '1') + '; }\n')
            shim = root / 'shim'
            shim.write_text(f'#!{sys.executable}\n' + '''import os,pathlib,sys
root=pathlib.Path(os.environ['CONFIRM_TEST_ROOT'])
ticks=int((root/'ticks').read_text())
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
failed=os.environ['CONTROL_FAILED']=='1'
if name=='systemctl':
 if 'reefy-verity-monitor' in args[-1]:
  if args[0]=='show': print('1' if os.environ['MONITOR_RESTARTED']=='1' else '0'); sys.exit(0)
  sys.exit(3 if os.environ['MONITOR_FAILED']=='1' or (ticks>=2 and os.environ['MONITOR_FAILS_AFTER_WAIT']=='1') else 0)
 if args[0]=='show': print('active' if 'reefy-storage' in args[-1] or (ticks>=2 and not failed) else ('failed' if failed else 'activating'))
 elif args[0]=='is-active': sys.exit(0 if 'reefy-storage' in args[-1] or (ticks>=2 and not failed) else 3)
 elif args[0]=='is-failed': sys.exit(0 if failed and args[-1]=='reefy-control' else 1)
 elif args[0]=='stop': (root/'stopped').touch()
elif name=='dmsetup': print('0 32 verity '+('C' if ticks>=2 and os.environ['CORRUPT_AFTER_WAIT']=='1' else 'V'))
elif name=='sleep': (root/'ticks').write_text(str(ticks+1))
elif name=='reefy-efi': (root/'confirmed').touch()
elif name=='busybox':
 if ticks<2 or failed: sys.exit(77)
 (root/'cleared').touch()
''')
            shim.chmod(0o755)
            for name in ('systemctl','dmsetup','sleep','reefy-efi'):
                (root / name).symlink_to(shim)
            (root / 'runtime/bin/busybox').symlink_to(shim)
            script = (SCRIPTS.parent / 'rootfs-overlay/usr/bin/reefy-boot-confirm').read_text()
            script = script.replace('/usr/lib/reefy/boot-slot-state.sh', str(root / 'slot.sh'))
            script = script.replace('/run/reefy-verity/runtime', str(root / 'runtime'))
            result = subprocess.run(['sh','-c',script], capture_output=True, text=True,
                timeout=20, env={**os.environ, 'PATH':str(root)+':'+os.environ['PATH'],
                    'CONFIRM_TEST_ROOT':str(root), 'CONTROL_FAILED':str(int(control_failed)),
                    'CORRUPT_AFTER_WAIT':str(int(corrupt_after_wait)),
                    'MONITOR_FAILED':str(int(monitor_failed)),
                    'MONITOR_FAILS_AFTER_WAIT':str(int(monitor_fails_after_wait)),
                    'MONITOR_RESTARTED':str(int(monitor_restarted))})
            return result, {name:(root/name).exists() for name in ('cleared','confirmed','stopped')}, int((root/'ticks').read_text())

    def test_default_slot_waits_for_control_before_clearing_failure_history(self):
        result, state, ticks = self.exercise()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(ticks, 2)
        self.assertTrue(state['cleared'])

    def test_corruption_during_health_wait_cannot_be_confirmed(self):
        result, state, _ = self.exercise(default=False, corrupt_after_wait=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(state['confirmed'])
        self.assertFalse(state['cleared'])
        self.assertFalse(state['stopped'])

    def test_default_slot_with_failed_control_preserves_failure_history(self):
        result, state, _ = self.exercise(control_failed=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(state['confirmed'])
        self.assertFalse(state['cleared'])

class HealthGateTests(unittest.TestCase):
    exercise = ConfirmationTests.exercise

    def test_unavailable_monitor_cannot_confirm_trial(self):
        result, state, _ = self.exercise(default=False, monitor_failed=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(state['confirmed'])
        self.assertFalse(state['cleared'])
        self.assertFalse(state['stopped'])

    def test_unavailable_monitor_cannot_clear_default_history(self):
        result, state, _ = self.exercise(monitor_failed=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(state['confirmed'])
        self.assertFalse(state['cleared'])

    def test_monitor_failure_during_wait_cannot_be_confirmed(self):
        result, state, _ = self.exercise(default=False, monitor_fails_after_wait=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(state['confirmed'])
        self.assertFalse(state['cleared'])
        self.assertFalse(state['stopped'])

    def test_restarted_monitor_cannot_confirm_trial(self):
        result, state, _ = self.exercise(default=False, monitor_restarted=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(state['confirmed'])
        self.assertFalse(state['cleared'])
        self.assertFalse(state['stopped'])

class ClosureTests(unittest.TestCase):
    def test_absolute_library_aliases_stay_inside_the_initramfs(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root, destination = Path(tmp)/'target', Path(tmp)/'initramfs'
            (root/'usr/lib').mkdir(parents=True)
            (root/'lib').symlink_to('/usr/lib')
            (root/'lib64').symlink_to('lib')
            (root/'usr/lib/probe.so').write_bytes(b'synthetic target ELF')
            with patch.object(copy.subprocess, 'run', return_value=SimpleNamespace(stdout='')):
                copy.copy_tools(root, destination, 'readelf', ['/usr/lib/probe.so'])
            self.assertTrue((destination/'lib').is_symlink())
            self.assertEqual((destination/'lib').readlink(), Path('usr/lib'))
            self.assertEqual((destination/'lib64/probe.so').read_bytes(), b'synthetic target ELF')
            self.assertEqual((root/'usr/lib/probe.so').read_bytes(), b'synthetic target ELF')

    @unittest.skipUnless(__import__('sys').platform == 'linux' and __import__('os').geteuid() == 0
                         and shutil.which('gcc') and shutil.which('readelf'),
                         'requires native Linux ELF tools and disposable root chroot')
    def test_real_elf_loader_resolves_the_copied_library_layout(self):
        import re
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, destination = base/'target', base/'initramfs'
            (root/'usr/lib').mkdir(parents=True)
            (root/'usr/bin').mkdir()
            (root/'lib').symlink_to('/usr/lib')
            (root/'lib64').symlink_to('lib')
            library = base/'library.c'
            library.write_text('int closure_fixture(void) { return 42; }\n')
            main = base/'main.c'
            main.write_text('extern int closure_fixture(void); int main(void) { return closure_fixture() != 42; }\n')
            subprocess.run(['gcc','-shared','-fPIC',str(library),'-Wl,-soname,libclosure-fixture.so',
                            '-o',str(root/'usr/lib/libclosure-fixture.so')],check=True)
            program = root/'usr/bin/probe'
            # Constrain lookup to /lib64, like a loader built for that target
            # layout. Do not let host fallback paths mask broken aliases.
            subprocess.run(['gcc',str(main),'-L'+str(root/'usr/lib'),'-lclosure-fixture',
                            '-Wl,-rpath,/lib64,-z,nodefaultlib','-o',str(program)],check=True)
            headers = subprocess.check_output(['readelf','-l',str(program)],text=True)
            interpreter = re.search(r'Requesting program interpreter: ([^\]]+)',headers)[1]
            shutil.copy2(interpreter,root/'usr/lib'/Path(interpreter).name)
            libc = subprocess.check_output(['gcc','-print-file-name=libc.so.6'],text=True).strip()
            shutil.copy2(libc,root/'usr/lib/libc.so.6')
            copy.copy_tools(root,destination,'readelf',['/usr/bin/probe'])
            result = subprocess.run(['chroot',str(destination),'/usr/bin/probe'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

class MonitorTests(unittest.TestCase):
    @unittest.skipUnless(__import__('sys').platform == 'linux' and __import__('os').geteuid() == 0
                         and shutil.which('busybox'), 'requires disposable Linux root and BusyBox')
    def test_monitor_path_is_independent_of_service_manager(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'bin').mkdir()
            (root/'sbin').mkdir()
            shutil.copy2(shutil.which('busybox'), root/'bin/busybox')
            shutil.copy2(Path(__file__).resolve().parents[1]/'scripts/verity-monitor.sh', root/'verity-monitor.sh')
            (root/'sbin/dmsetup').write_text("#!/bin/busybox sh\n[ ! -e /checked ] || exit 1\ntouch /checked\necho '0 8 verity V'\n")
            (root/'sbin/dmsetup').chmod(0o755)
            (root/'verity-recover.sh').write_text("printf recovered > /recovered\n")
            result = subprocess.run([shutil.which('chroot'), str(root), '/bin/busybox',
                'sh', '/verity-monitor.sh'], env={**os.environ, 'PATH':'/usr/bin:/usr/sbin'},
                text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root/'recovered').read_text(), 'recovered')

if __name__ == '__main__':
    unittest.main()
