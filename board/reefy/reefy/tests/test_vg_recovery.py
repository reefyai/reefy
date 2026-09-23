"""Negative controls for the synthetic reconstruction planner."""
import copy
import unittest
import _bootstrap
from reefy.vg_recovery_plan import candidate, Refused, render
from reefy.lvm_text import parse


def fixture():
    def lv(identity, kind, **extra):
        return {'id': identity, 'status': ['READ', 'WRITE'], 'segment_count': 1,
                'segment1': {'start_extent': 0, 'extent_count': 16, 'type': kind, **extra}}
    lvs = {
        'reefy_state': lv('state', 'striped', stripe_count=1, stripes=['pv0', 0]),
        'tmeta': lv('meta', 'striped', stripe_count=1, stripes=['pv0', 16]),
        'tdata': lv('data', 'striped', stripe_count=1, stripes=['pv0', 32]),
        'reefy_pool': lv('pool', 'thin-pool', metadata='tmeta', pool='tdata', chunk_size=1024, transaction_id=4),
        'reefy_default': lv('default', 'thin', thin_pool='reefy_pool', device_id=1, transaction_id=0),
        'old': lv('snapshot', 'thin', thin_pool='reefy_pool', device_id=2, transaction_id=3, origin='reefy_default'),
    }
    record = {'reefy': {'id': 'vg', 'seqno': 9, 'extent_size': 8192,
                        'physical_volumes': {'pv0': {'id': 'pv', 'pe_start': 2048, 'pe_count': 100}},
                        'logical_volumes': lvs}}
    xml = ('<superblock transaction="8" data_block_size="1024" nr_data_blocks="128">'
           '<device dev_id="1" transaction="0" mapped_blocks="2"/>'
           '<device dev_id="2" transaction="7" mapped_blocks="2"/>'
           '</superblock>')
    return record, xml


class ReconstructionTests(unittest.TestCase):
    def test_quarantine_drops_stale_provenance(self):
        record, xml = fixture()
        original = copy.deepcopy(record)
        result, names = candidate([record], xml)
        self.assertEqual(record, original)
        self.assertEqual(names, ['recovered_unattributed_2'])
        lvs = result['reefy']['logical_volumes']
        self.assertNotIn('old', lvs)
        self.assertNotIn('origin', lvs[names[0]]['segment1'])
        self.assertEqual(lvs[names[0]]['status'], ['READ', 'VISIBLE'])
        self.assertEqual(lvs[names[0]]['flags'], ['ACTIVATION_SKIP'])
        self.assertEqual(lvs['reefy_pool']['segment1']['transaction_id'], 8)
        self.assertEqual(parse(render(result)), result)

    def test_refuses_changed_persistent_identity(self):
        record, xml = fixture()
        record['reefy']['logical_volumes']['reefy_default']['segment1']['transaction_id'] = 5
        with self.assertRaises(Refused):
            candidate([record], xml)

    def test_refuses_conflicting_history(self):
        record, xml = fixture()
        older = copy.deepcopy(record)
        older['reefy']['logical_volumes']['reefy_default']['segment1']['extent_count'] += 1
        with self.assertRaises(Refused):
            candidate([older, record], xml)

    def test_refuses_bad_inventory_and_geometry(self):
        record, xml = fixture()
        for old, new in [('dev_id="2"', 'dev_id="3"'),
                         ('dev_id="2"', 'dev_id="1"'),
                         ('transaction="8"', 'transaction="2"'),
                         ('data_block_size="1024"', 'data_block_size="512"'),
                         ('mapped_blocks="2"', 'mapped_blocks="999"'),
                         ('nr_data_blocks="128"', 'nr_data_blocks="129"')]:
            with self.subTest(change=new), self.assertRaises(Refused):
                candidate([record], xml.replace(old, new))

    def test_refuses_overlapping_physical_extents(self):
        record, xml = fixture()
        record['reefy']['logical_volumes']['tmeta']['segment1']['stripes'][1] = 8
        with self.assertRaises(Refused):
            candidate([record], xml)

    def test_parser_rejects_incomplete_or_ignored_bytes(self):
        for text in ['a { b = 1', 'a = 1 a = 2', 'a = 1 @', 'a = [1 2]', 'a = [1,]', 'a { } }']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse(text)


if __name__ == '__main__':
    unittest.main()

class HeaderTests(unittest.TestCase):
    def prefix(self):
        import struct
        from reefy.vg_recovery import crc
        data = bytearray(1024 * 1024)
        data[512:520] = b'LABELONE'
        struct.pack_into('<Q', data, 520, 1)
        struct.pack_into('<I', data, 532, 32)
        data[536:544] = b'LVM2 001'
        data[544:576] = b'a' * 32
        struct.pack_into('<Q', data, 576, 16 * 1024**3)
        struct.pack_into('<QQ', data, 584, len(data), 0)
        struct.pack_into('<QQ', data, 616, 4096, len(data) - 4096)
        struct.pack_into('<I', data, 528, crc(data[532:1024]))
        data[4100:4116] = b' LVM2 x[5A%r0N*>'
        struct.pack_into('<IQQ', data, 4116, 1, 4096, len(data) - 4096)
        struct.pack_into('<QQII', data, 4136, 512, 20, 1234, 0)
        struct.pack_into('<I', data, 4096, crc(data[4100:4608]))
        return data

    def test_headers_and_crc_reject_modified_geometry(self):
        from reefy.vg_recovery import headers
        data = self.prefix()
        self.assertEqual(headers(data, 16 * 1024**3)['data_start'], len(data))
        data[616] ^= 1
        with self.assertRaisesRegex(Refused, 'checksum'):
            headers(data, 16 * 1024**3)

    def test_ring_wrap(self):
        from reefy.vg_recovery import headers, ring_read
        data = self.prefix()
        info = headers(data, 16 * 1024**3)
        data[-4:] = b'abcd'
        data[4608:4612] = b'efgh'
        self.assertEqual(ring_read(data, info, info['mda_size'] - 4, 8), b'abcdefgh')

    def test_valid_committed_record_is_never_rolled_back(self):
        from reefy.vg_recovery import headers, records_from_ring, crc
        data = self.prefix()
        info = headers(data, 16 * 1024**3)
        info['checksum'] = crc(data[4608:4628])
        with self.assertRaisesRegex(Refused, 'valid checksum'):
            records_from_ring(data, info)
