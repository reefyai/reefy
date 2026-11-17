"""A timed-out storage child may have completed its kernel-side operation."""
import builtins
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import _bootstrap
from reefy import storage


def result(code=0, stdout=''):
    return types.SimpleNamespace(returncode=code, stdout=stdout, stderr='')


class ProvisionTimeoutPostconditions(unittest.TestCase):
    def seed_mount(self, completed, *, unreadable=False, child=False):
        calls=[]
        with tempfile.TemporaryDirectory() as directory:
            info=Path(directory)/'mountinfo'
            target=os.path.realpath('/tmp/synthetic-seed')
            root_mount='1 0 0:0 / / rw - rootfs rootfs rw\n'
            info.write_text(root_mount+'24 1 0:1 / '+target+' rw - xfs /dev/synthetic/state rw\n')
            def run(args, **kwargs):
                calls.append(args)
                if args[0]=='mountpoint':return result(1)
                if args[0]=='blkid':return result(stdout='xfs\n')
                if args[0]=='umount':
                    info.write_text(root_mount+('' if completed else '24 1 0:1 / '+target+('/child' if child else '')+' rw - xfs /dev/synthetic/state rw\n'))
                    raise subprocess.TimeoutExpired(args,10)
                return result()
            def read(path,*args,**kwargs):
                if str(path)=='/proc/self/mountinfo':
                    if unreadable:raise PermissionError('synthetic unavailable mount table')
                    return builtins.open(info,*args,**kwargs)
                return builtins.open(path,*args,**kwargs)
            with mock.patch.object(storage.subprocess,'run',side_effect=run), mock.patch.object(storage,'open',side_effect=read,create=True), mock.patch.object(storage.os.path,'exists',return_value=True), mock.patch.object(storage.os.path,'isdir',return_value=True), mock.patch.object(storage.os,'makedirs'), mock.patch.object(storage.os,'listdir',return_value=[]), mock.patch.object(storage.os,'rmdir'), mock.patch.object(storage.tempfile,'mkdtemp',return_value=target):
                storage.Storage()._mount_state_lv()
        return calls

    def test_completed_unmount_continues_to_final_mount_without_replay(self):
        calls=self.seed_mount(True)
        self.assertEqual(sum(c[0]=='umount' for c in calls),1)
        self.assertTrue(any(c[0]=='mount' and c[-1].endswith('/state') for c in calls))

    def test_still_mounted_or_unobservable_tree_preserves_original_timeout(self):
        for options in ({}, {'unreadable':True}, {'child':True}):
            with self.subTest(options=options), self.assertRaises(subprocess.TimeoutExpired):
                self.seed_mount(False,**options)

    def wipe(self, completed, *, report=None):
        with tempfile.TemporaryDirectory() as directory:
            disk=Path(directory)/'target';disk.write_bytes(b'synthetic signature')
            sibling=Path(directory)/'unrelated';sibling.write_bytes(b'preserve sibling data')
            calls=[]
            def run(args, **kwargs):
                calls.append(args)
                if args[:2]==['wipefs','-af']:
                    if completed:disk.write_bytes(b'')
                    raise subprocess.TimeoutExpired(args,15)
                if args[0]=='wipefs':
                    return result(stdout=report if report is not None else json.dumps({'signatures':[] if completed else [{'type':'xfs'}]}))
                if args[0]=='dd':disk.write_bytes(b'zeroed header')
                return result()
            with mock.patch.object(storage.subprocess,'run',side_effect=run), mock.patch.object(storage.Storage,'_get_dm_tree',return_value=[]):
                try:storage.Storage()._force_wipe_device(str(disk))
                finally:
                    self.assertEqual(sibling.read_bytes(),b'preserve sibling data')
                    self.assertEqual(sum(c[:2]==['wipefs','-af'] for c in calls),1)
                    if not completed or report is not None:self.assertFalse(any(c[0]=='dd' for c in calls))
            self.assertEqual(disk.read_bytes(),b'zeroed header')

    def test_completed_signature_wipe_continues_without_second_destructive_wipe(self):
        self.wipe(True)

    def test_remaining_or_unknown_signatures_stop_before_zeroing(self):
        for completed,report in ((False,None),(True,'{}'),(True,'not json'),(True,'{"signatures":null}')):
            with self.subTest(report=report), self.assertRaises(subprocess.TimeoutExpired):
                self.wipe(completed,report=report)
