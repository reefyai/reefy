"""Synthetic sysfs/mount graphs exercise proposed isolation without live devices."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('build_storage', Path(__file__).with_name('build_storage.py'))
storage = importlib.util.module_from_spec(spec); spec.loader.exec_module(storage)


class BuildStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.build = self.root/'build'; self.vm = self.root/'vm'
        self.build.mkdir(); self.vm.mkdir()
        self.sys = self.root/'sys'; (self.sys/'dev/block').mkdir(parents=True)
        self.nodes = {}
        self.device('nvme0n1', '259:0')
        self.device('nvme1n1', '259:1')
        self.device('nvme2n1', '259:2')
        self.device('dm-0', '253:0', ['259:0', '259:1'])
        self.device('dm-1', '253:1', ['253:0'])
        self.device('dm-2', '253:2', ['259:2'])
        self.layout = storage.layout(str(self.build), [str(self.vm)])
        for role in ('primary','secondary'):
            for layer in ('diff','work','lower'):
                (self.build/'runner-engine/overlay2'/role/layer).mkdir(parents=True)
        for override in self.layout['compose_overrides'].values():
            for bind in override['services']['app']['volumes']:
                Path(bind['source']).mkdir(parents=True, exist_ok=True)
        self.mounts = self.mount('253:2', self.build) + self.mount('253:1', self.vm)
        self.capacity = SimpleNamespace(f_blocks=2048, f_frsize=storage.GIB, f_bavail=1024)
        self.sentinel = self.vm/'sentinel'; self.sentinel.write_bytes(b'preserved VM data')
    def mount(self, device, target, fs='ext4', mode='rw'):
        escaped = str(target).replace(' ', r'\040')
        return f'1 0 {device} / {escaped} {mode},relatime - {fs} /dev/example {mode}\n'
    def device(self, name, number, children=()):
        node = self.sys/'devices'/name; node.mkdir(parents=True)
        (node/'dev').write_text(number)
        (node/'slaves').mkdir()
        (self.sys/'dev/block'/number).symlink_to(node)
        self.nodes[number] = node
        if name.startswith('nvme'):
            controller = self.sys/'controllers'/name.rsplit('n', 1)[0]
            controller.mkdir(parents=True, exist_ok=True)
            (node/'device').symlink_to(controller)
        for child in children:
            (node/'slaves'/self.nodes[child].name).symlink_to(self.nodes[child])
        return node
    def check(self, layout=None, mounts=None):
        return storage.check(layout or self.layout, self.mounts if mounts is None else mounts,
                             self.sys, lambda _: self.capacity)
    def test_independent_devices_pass_and_existing_data_is_untouched(self):
        result = self.check()
        self.assertTrue(result['storage_ready'])
        self.assertFalse(result['configuration_applied'])
        self.assertFalse(result['qualification_passed'])
        self.assertEqual(result['build']['leaves'], ['nvme:nvme2'])
        self.assertEqual(result['vm'][0]['leaves'], ['nvme:nvme0', 'nvme:nvme1'])
        self.assertEqual(self.sentinel.read_bytes(), b'preserved VM data')
    def test_another_lv_on_same_thin_pool_is_rejected(self):
        self.device('dm-3','253:3',['253:0'])
        with self.assertRaisesRegex(storage.StorageError, 'share physical'):
            self.check(mounts=self.mount('253:3',self.build)+self.mount('253:1',self.vm))
        self.assertEqual(self.sentinel.read_bytes(), b'preserved VM data')
    def test_different_partitions_on_same_disk_are_rejected(self):
        parent=self.nodes['259:2']
        for name,number in [('nvme2n1p1','259:3'),('nvme2n1p2','259:4')]:
            p=parent/name;p.mkdir();(p/'partition').write_text('1');(p/'dev').write_text(number)
            (self.sys/'dev/block'/number).symlink_to(p)
        with self.assertRaisesRegex(storage.StorageError,'share physical'):
            self.check(mounts=self.mount('259:3',self.build)+self.mount('259:4',self.vm))
    def test_namespaces_on_one_nvme_controller_are_not_independent(self):
        self.device('nvme2n2', '259:5')
        with self.assertRaisesRegex(storage.StorageError, 'share physical'):
            self.check(mounts=self.mount('259:2',self.build)+self.mount('259:5',self.vm))

    def test_directory_on_parent_filesystem_is_not_a_dedicated_mount(self):
        with self.assertRaisesRegex(storage.StorageError,'independently mounted'):
            self.check(mounts=self.mount('253:2',self.root))
    def test_memory_overlay_network_and_readonly_mounts_fail_closed(self):
        for fs,mode in [('tmpfs','rw'),('overlay','rw'),('nfs4','rw'),('ext4','ro')]:
            with self.subTest(fs=fs,mode=mode), self.assertRaises(storage.StorageError):
                self.check(mounts=self.mount('253:2',self.build,fs,mode)+self.mount('253:1',self.vm))
    def test_missing_or_cyclic_graphs_fail_closed(self):
        (self.nodes['253:2']/'slaves/nvme2n1').unlink()
        with self.assertRaisesRegex(storage.StorageError,'unknown or virtual'):
            self.check()
        (self.nodes['253:2']/'slaves/self').symlink_to(self.nodes['253:2'])
        with self.assertRaisesRegex(storage.StorageError,'cyclic'):
            self.check()
    def test_insufficient_capacity_or_reserve_is_rejected(self):
        for size,free in [(1000,900),(2048,400)]:
            self.capacity.f_blocks=size;self.capacity.f_bavail=free
            with self.subTest(size=size,free=free), self.assertRaisesRegex(storage.StorageError,'capacity'):
                self.check()
    def test_missing_directory_and_symlink_escape_are_rejected(self):
        directory=self.build/'primary/work';directory.rmdir()
        with self.assertRaisesRegex(storage.StorageError,'missing'):
            self.check()
        directory.symlink_to(self.vm,target_is_directory=True)
        with self.assertRaisesRegex(storage.StorageError,'escapes'):
            self.check()
    def test_nested_submount_on_vm_device_is_rejected(self):
        mounts=self.mounts+self.mount('253:1',self.build/'primary/work')
        with self.assertRaisesRegex(storage.StorageError,'escapes'):
            self.check(mounts=mounts)
    def test_edited_specs_cannot_disable_capacity_or_add_privilege_mounts(self):
        edited=copy.deepcopy(self.layout);edited['minimum_build_bytes']=1
        with self.assertRaisesRegex(storage.StorageError,'reviewed generated shape'):
            self.check(layout=edited)
        edited=copy.deepcopy(self.layout)
        edited['compose_overrides']['primary']['services']['app']['volumes'].append(
            dict(type='bind',source='/var/run/docker.sock',target='/var/run/docker.sock',read_only=False))
        with self.assertRaises(storage.StorageError):self.check(layout=edited)
    def observed(self):
        return {role:dict(
                    Mounts=[dict(Destination=b['target'],Source=b['source'],
                                 RW=not b['read_only'],Type='bind')
                            for b in override['services']['app']['volumes']],
                    GraphDriver=dict(Name='overlay2',Data=dict(
                        UpperDir=str(self.build/'runner-engine/overlay2'/role/'diff'),
                        WorkDir=str(self.build/'runner-engine/overlay2'/role/'work'),
                        LowerDir=str(self.build/'runner-engine/overlay2'/role/'lower'))))
                for role,override in self.layout['compose_overrides'].items()}
    def check_containers(self, observed):
        return storage.check_container_mounts(self.layout,observed,self.mounts,self.sys)
    def test_renderer_keeps_private_caches_readonly_seed_and_shared_counter(self):
        rows=self.layout['compose_overrides']
        primary=rows['primary']['services']['app']['volumes']
        secondary=rows['secondary']['services']['app']['volumes']
        a=next(x for x in primary if x['target']=='/buildroot-cache')
        b=next(x for x in secondary if x['target']=='/buildroot-cache')
        self.assertNotEqual(a['source'],b['source'])
        self.assertEqual({x['target'] for x in primary},
                         {'/buildroot-cache','/_work','/actions-runner/_diag',
                          '/tmp','/var/tmp','/var/lib/docker','/var/lib/containerd',
                          '/buildroot-cache/shared-version'})
        seed=next(x for x in secondary if x['target']=='/seed-cache')
        self.assertTrue(seed['read_only']);self.assertEqual(seed['source'],a['source'])
        counters=[next(x for x in r if x['target']=='/buildroot-cache/shared-version') for r in (primary,secondary)]
        self.assertEqual(counters[0],counters[1]);self.assertFalse(counters[0]['read_only'])
        self.assertTrue(self.check_containers(self.observed())['container_mounts_match'])
        self.assertTrue(all(x['bind']['create_host_path'] is False for x in primary+secondary))
        self.assertNotIn('environment',rows['primary']['services']['app'])
        self.assertNotIn('privileged',rows['primary']['services']['app'])
    def test_container_inspection_detects_unmoved_workspace_and_wrong_seed_mode(self):
        for target in ('/_work','/seed-cache'):
            observed=self.observed();role='secondary'
            row=next(x for x in observed[role]['Mounts'] if x['Destination']==target)
            if target=='/_work':row['Source']=str(self.vm)
            else:row['RW']=True
            with self.subTest(target=target), self.assertRaises(storage.StorageError):
                self.check_containers(observed)
    def test_old_container_mounts_cannot_pass_as_a_completed_cutover(self):
        observed=self.observed()
        for role in observed:
            observed[role]['Mounts']=[r for r in observed[role]['Mounts'] if r['Destination'] in
                            ('/buildroot-cache','/var/lib/docker','/var/lib/containerd')]
        with self.assertRaisesRegex(storage.StorageError,'missing builder bulk-write mount'):
            self.check_containers(observed)

    def test_malformed_layout_cli_fails_with_structured_result(self):
        specfile=self.root/'bad.json';specfile.write_text('[]')
        result=subprocess.run([sys.executable,str(Path(storage.__file__)),'check',str(specfile)],
                              capture_output=True,text=True)
        self.assertEqual(result.returncode,1)
        self.assertFalse(json.loads(result.stdout)['storage_ready'])
        self.assertFalse(json.loads(result.stdout)['configuration_applied'])
        self.assertNotIn('Traceback',result.stderr)

    def test_unmoved_outer_writable_layer_is_rejected(self):
        for key in ('UpperDir','WorkDir','LowerDir'):
            observed=self.observed()
            observed['primary']['GraphDriver']['Data'][key]=str(self.vm)
            with self.subTest(key=key), self.assertRaisesRegex(storage.StorageError,'writable layer escapes'):
                self.check_containers(observed)

    def test_unknown_outer_snapshotter_cannot_claim_verified_backing(self):
        observed=self.observed();observed['primary']['GraphDriver']={'Name':'unknown','Data':{}}
        with self.assertRaisesRegex(storage.StorageError,'writable layer backing is unverified'):
            self.check_containers(observed)

    def test_unplanned_engine_socket_is_rejected(self):
        observed=self.observed();observed['primary']['Mounts'].append(dict(Destination='/var/run/docker.sock',Source='/var/run/docker.sock',RW=True,Type='bind'))
        with self.assertRaisesRegex(storage.StorageError,'engine socket'):
            self.check_containers(observed)
    def test_bad_mount_inventory_and_missing_sysfs_are_not_accepted(self):
        with self.assertRaises(storage.StorageError):self.check(mounts='not a mount inventory')
        (self.sys/'dev/block/253:2').unlink()
        with self.assertRaises(storage.StorageError):self.check()
    def test_cli_plan_is_nonmutating_and_check_refuses_unmounted_target(self):
        result=subprocess.run([sys.executable,str(Path(storage.__file__)),'plan',
                               '--build-root',str(self.root/'not-created'),
                               '--vm-root',str(self.vm)],capture_output=True,text=True,check=True)
        value=json.loads(result.stdout)
        self.assertFalse(value['storage_ready']);self.assertFalse(value['configuration_applied'])
        self.assertFalse((self.root/'not-created').exists())
        specfile=self.root/'layout.json';specfile.write_text(result.stdout)
        result=subprocess.run([sys.executable,str(Path(storage.__file__)),'check',str(specfile)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(json.loads(result.stdout)['storage_ready'])
        self.assertEqual(self.sentinel.read_bytes(),b'preserved VM data')


if __name__=='__main__':unittest.main()
