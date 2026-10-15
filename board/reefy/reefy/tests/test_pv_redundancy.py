"""Background redundancy detection, native repair and bounded-failure controls."""
import contextlib
import copy
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap
from reefy import pv_redundancy as recovery
from reefy.vg_recovery_plan import Refused, render
from test_vg_recovery import fixture

MIB = 1024 ** 2
SIZE = 16 * MIB
PV = '/dev/mapper/reefy-synthetic'


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.disk = self.root / 'pv'
        with self.disk.open('wb') as stream:
            stream.truncate(SIZE)
        self.record, _ = fixture()
        vg = self.record['reefy']
        vg['extent_size'] = 128
        vg['physical_volumes']['pv0'].update(id='a' * 32, pe_start=2048,
                                            pe_count=128, dev_size=SIZE // 512)
        self.text = render(self.record).encode() + b'\0'
        label = bytearray(512)
        label[:8] = b'LABELONE'
        struct.pack_into('<Q', label, 8, 1)
        struct.pack_into('<I', label, 20, 32)
        label[24:32] = b'LVM2 001'
        label[32:64] = b'a' * 32
        struct.pack_into('<Q', label, 64, SIZE)
        struct.pack_into('<QQ', label, 72, MIB, 0)
        struct.pack_into('<QQ', label, 104, 4096, MIB - 4096)
        struct.pack_into('<QQ', label, 120, SIZE - MIB, MIB)
        struct.pack_into('<I', label, 16, recovery.crc(label[20:]))
        self.write(512, label)
        self.locations = ((4096, MIB - 4096), (SIZE - MIB, MIB))
        for index in (0, 1):
            self.area(index, self.text)
        self.calls = []
        self.native_failure = None
        original_read = recovery.read_pv
        self.addCleanup(patch.stopall)
        patch.object(recovery, 'read_pv', side_effect=lambda pv, size: original_read(self.disk, size)).start()
        self.mode = 'repair'
        self.repair_count = 0
        self.discovered = [PV]
        self.sleep = patch.object(recovery.time, 'sleep').start()
        patch.object(recovery, 'run', side_effect=self.native).start()

    def write(self, offset, data):
        with self.disk.open('r+b') as stream:
            stream.seek(offset)
            stream.write(data)

    def area(self, index, text, *, offset=512):
        start, size = self.locations[index]
        raw = bytearray(size)
        raw[4:20] = b' LVM2 x[5A%r0N*>'
        struct.pack_into('<IQQ', raw, 20, 1, start, size)
        struct.pack_into('<QQII', raw, 40, offset, len(text), recovery.crc(text), 0)
        first = min(len(text), size - offset)
        raw[offset:offset + first] = text[:first]
        raw[512:512 + len(text) - first] = text[first:]
        struct.pack_into('<I', raw, 0, recovery.crc(raw[4:512]))
        self.write(start, raw)

    def damage(self, index):
        start, _ = self.locations[index]
        with self.disk.open('r+b') as stream:
            stream.seek(start + 512)
            old = stream.read(1)
            stream.seek(start + 512)
            stream.write(bytes([old[0] ^ 255]))

    def native(self, args, check=True):
        self.calls.append(args)
        if args[0] == 'blockdev':
            return subprocess.CompletedProcess(args, 0, str(SIZE), '')
        if args[0] == 'pvs':
            if self.mode == 'busy':
                return subprocess.CompletedProcess(args, 5, '', 'Cannot lock reefy')
            rows = [{'pv_name': pv, 'vg_name': 'reefy'} for pv in self.discovered]
            return subprocess.CompletedProcess(args, 0, json.dumps({'report': [{'pv': rows}]}), '')
        self.assertEqual(args[0], 'vgck')
        self.assertIn('wait_for_locks=0', args[args.index('--config') + 1])
        self.assertNotIn('--nolocking', args)
        self.repair_count += 1
        if self.mode == 'error':
            return subprocess.CompletedProcess(args, 5, '', 'I/O error')
        if self.mode == 'never' or (self.mode == 'retry' and self.repair_count == 1):
            return subprocess.CompletedProcess(args, 0, '', 'not repairing bad metadata (0xb)')
        current = recovery.read_pv(PV, SIZE)
        valid = next(area for area in current['areas'] if area['valid'])
        for index in (0, 1):
            self.area(index, valid['text'])
        if self.mode == 'changed-label':
            with self.disk.open('rb') as stream:
                stream.seek(512)
                label = bytearray(stream.read(512))
            label[32:64] = b'b' * 32
            struct.pack_into('<I', label, 16, recovery.crc(label[20:]))
            self.write(512, label)
        return subprocess.CompletedProcess(args, 0, '', '')

    def test_repairs_either_current_copy_while_active(self):
        for damaged in (0, 1):
            self.area(0, self.text)
            self.area(1, self.text)
            self.damage(damaged)
            result = recovery.recover()
            self.assertEqual(result['outcome'], 'repaired')
            self.assertTrue(all(a['valid'] for a in recovery.read_pv(PV, SIZE)['areas']))
            self.assertEqual(len(result['attempts']), 1)

    def test_healthy_storage_does_not_request_metadata_writes(self):
        result = recovery.recover()
        self.assertEqual(result['outcome'], 'healthy')
        self.assertEqual(result['writes_requested'], 0)
        self.assertEqual(self.repair_count, 0)

    def test_false_success_is_verified_then_retried(self):
        self.damage(1)
        self.mode = 'retry'
        result = recovery.recover()
        self.assertEqual(result['outcome'], 'repaired')
        self.assertEqual(result['writes_requested'], 2)
        self.assertFalse(result['attempts'][0]['verified'])
        self.assertTrue(result['attempts'][1]['verified'])
        self.sleep.assert_called_once_with(1)

    def test_three_false_successes_leave_degraded_result(self):
        self.damage(1)
        self.mode = 'never'
        result = recovery.recover()
        self.assertEqual(result['outcome'], 'degraded')
        self.assertEqual(result['writes_requested'], 3)
        self.assertEqual(self.repair_count, 3)
        self.assertEqual([a['valid'] for a in recovery.read_pv(PV, SIZE)['areas']], [True, False])
        self.assertEqual(self.sleep.call_count, 2)

    def test_busy_lvm_does_not_wait_or_request_a_write(self):
        self.damage(1)
        self.mode = 'busy'
        result = recovery.recover()
        self.assertEqual(result['outcome'], 'deferred')
        self.assertEqual(result['writes_requested'], 0)
        self.assertEqual(len(result['attempts']), 3)
        self.assertEqual(self.repair_count, 0)
        for args in self.calls:
            if args[0] == 'pvs':
                self.assertIn('wait_for_locks=0', args[2])

    def test_operational_failure_is_not_retried(self):
        self.damage(1)
        self.mode = 'error'
        result = recovery.recover()
        self.assertEqual(result['outcome'], 'deferred')
        self.assertEqual(self.repair_count, 1)
        self.sleep.assert_not_called()

    def test_changed_pv_identity_is_not_claimed_as_success(self):
        self.damage(1)
        self.mode = 'changed-label'
        result = recovery.recover()
        self.assertEqual(result['outcome'], 'deferred')
        self.assertIn('identity', result['attempts'][0]['verification_error'])
        self.assertEqual(self.repair_count, 1)

    def test_ambiguous_or_unrecoverable_text_does_not_write(self):
        self.damage(0)
        self.damage(1)
        self.assertEqual(recovery.recover()['outcome'], 'deferred')
        self.area(0, self.text)
        self.area(1, self.text.replace(b'seqno = 9', b'seqno = 8'))
        self.assertEqual(recovery.recover()['outcome'], 'deferred')
        self.assertEqual(self.repair_count, 0)

    def test_damaged_header_and_foreign_identity_do_not_write(self):
        self.damage(1)
        self.write(self.locations[1][0] + 4, b'!')
        self.assertEqual(recovery.recover()['outcome'], 'deferred')
        self.area(1, self.text)
        self.damage(1)
        self.area(0, self.text.replace(b'a' * 32, b'b' * 32))
        self.assertEqual(recovery.recover()['outcome'], 'deferred')
        self.assertEqual(self.repair_count, 0)

    def test_damaged_multi_pv_storage_does_not_write(self):
        self.damage(1)
        self.discovered = [PV, PV]
        self.assertEqual(recovery.recover()['outcome'], 'deferred')
        self.assertEqual(self.repair_count, 0)

    def test_missing_storage_is_noop(self):
        self.discovered = []
        self.assertEqual(recovery.recover()['outcome'], 'healthy')
        self.assertEqual(self.repair_count, 0)

    def test_reads_wrapped_current_record(self):
        start, size = self.locations[0]
        self.area(0, self.text, offset=size - 512)
        snapshot = recovery.read_pv(PV, SIZE)
        self.assertTrue(snapshot['areas'][0]['valid'])
        self.assertEqual(snapshot['areas'][0]['text'], self.text)

    def test_legacy_single_area_is_not_rewritten(self):
        with self.disk.open('rb') as stream:
            stream.seek(512)
            label = bytearray(stream.read(512))
        label[120:136] = bytes(16)
        struct.pack_into('<I', label, 16, recovery.crc(label[20:]))
        self.write(512, label)
        self.damage(0)
        self.assertEqual(recovery.recover()['writes_requested'], 0)

    def test_extent_overlap_and_wrong_device_size_refuse_write(self):
        for field, value in (('pe_count', SIZE), ('dev_size', SIZE // 512 - 1)):
            bad = copy.deepcopy(self.record)
            bad['reefy']['physical_volumes']['pv0'][field] = value
            self.area(0, render(bad).encode() + b'\0')
            self.area(1, self.text)
            self.damage(1)
            self.assertEqual(recovery.recover()['outcome'], 'deferred')
        self.assertEqual(self.repair_count, 0)

    def test_reporting_failure_does_not_escape(self):
        with patch.object(recovery, 'RUN_STATUS', self.root / 'run.json'), \
                patch.object(recovery, 'STATUS', self.root / 'persistent.json'), \
                patch.object(recovery.os.path, 'ismount', return_value=True):
            recovery.save_result({'outcome': 'healthy'})
            self.assertEqual(json.loads((self.root / 'run.json').read_text())['outcome'], 'healthy')
        with patch.object(Path, 'mkdir', side_effect=OSError('full')):
            recovery.save_result({'outcome': 'deferred'})


if __name__ == '__main__':
    unittest.main()
