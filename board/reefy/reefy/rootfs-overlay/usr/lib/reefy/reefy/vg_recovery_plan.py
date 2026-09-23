"""Constrained reconstruction of a single-PV Reefy VG from surviving history.

The caller must validate headers, thin metadata and XFS geometry, preserve
evidence durably, and reject active PVs before applying a candidate.
"""
import copy
import json
import re
import uuid
import xml.etree.ElementTree as ET

from reefy.lvm_text import parse


class Refused(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def segment(lv):
    require(lv.get('segment_count') == 1, 'multiple LV segments')
    result = lv['segment1']
    require(result.get('start_extent') == 0, 'nonzero segment start')
    require(type(result.get('extent_count')) is int and result['extent_count'] > 0,
            'invalid LV size')
    return result


def layout(record):
    vg = record['reefy']
    require(len(vg['physical_volumes']) == 1, 'multi-PV VG')
    require(type(vg['extent_size']) is int and vg['extent_size'] > 0, 'extent size')
    lvs = vg['logical_volumes']
    require(3 <= len(lvs) <= 128, 'unsupported LV count')
    require({'reefy_state', 'reefy_default', 'reefy_pool'} <= lvs.keys(), 'required LVs missing')
    pool = segment(lvs['reefy_pool'])
    require(pool['type'] == 'thin-pool', 'not a thin pool')
    physical = {}
    ranges = []
    for name, lv in lvs.items():
        seg = segment(lv)
        require(seg['type'] in ('striped', 'thin', 'thin-pool'), 'unsupported segment type')
        if seg['type'] != 'striped':
            continue
        require(seg.get('stripe_count') == 1, 'striped data')
        pv_name, start = seg['stripes']
        require(pv_name in vg['physical_volumes'] and type(start) is int and start >= 0,
                'invalid physical extent')
        end = start + seg['extent_count']
        require(end <= vg['physical_volumes'][pv_name]['pe_count'], 'extent outside PV')
        require(all(end <= a or start >= b for a, b in ranges), 'overlapping extents')
        ranges.append((start, end))
        physical[name] = (lv['id'], seg)
    require(pool['metadata'] in physical and pool['pool'] in physical
            and 'reefy_state' in physical, 'missing physical volumes')
    return (vg['id'], vg['extent_size'], vg['physical_volumes'], physical,
            lvs['reefy_pool']['id'], pool['chunk_size'])


def candidate(records, thin_xml):
    require(bool(records), 'no surviving VG records')
    records = sorted(records, key=lambda item: item['reefy']['seqno'])
    reference = layout(records[-1])
    require(all(layout(item) == reference for item in records), 'conflicting physical geometry')
    latest = records[-1]
    vg = latest['reefy']
    lvs = vg['logical_volumes']
    pool = segment(lvs['reefy_pool'])
    root = ET.fromstring(thin_xml)
    require(root.tag == 'superblock', 'invalid thin dump')
    actual_transaction = int(root.attrib['transaction'])
    require(actual_transaction >= pool['transaction_id'], 'thin transaction moved backwards')
    require(int(root.attrib['data_block_size']) == pool['chunk_size'], 'chunk size mismatch')
    data_sectors = segment(lvs[pool['pool']])['extent_count'] * vg['extent_size']
    require(data_sectors % pool['chunk_size'] == 0, 'unaligned pool')
    require(int(root.attrib['nr_data_blocks']) == data_sectors // pool['chunk_size'], 'pool size mismatch')
    devices = {}
    for node in root.findall('device'):
        device_id = int(node.attrib['dev_id'])
        require(device_id not in devices, 'duplicate thin ID')
        devices[device_id] = node.attrib
    expected = {}
    for name, lv in lvs.items():
        seg = segment(lv)
        if seg['type'] == 'thin':
            require(seg['thin_pool'] == 'reefy_pool', 'foreign pool')
            require(seg['device_id'] not in expected, 'duplicate VG thin ID')
            expected[seg['device_id']] = name
    require(devices.keys() == expected.keys(), 'unknown or missing thin devices')
    result = copy.deepcopy(latest)
    recovered = result['reefy']['logical_volumes']
    quarantined = []
    for device_id, name in expected.items():
        old = lvs[name]
        seg = segment(old)
        transaction = int(devices[device_id]['transaction'])
        require(int(devices[device_id]['mapped_blocks']) * pool['chunk_size'] <=
                seg['extent_count'] * vg['extent_size'], 'mapped blocks exceed LV size')
        if 'origin' not in seg:
            require(transaction == seg['transaction_id'], 'persistent thin identity changed')
            for history in records:
                previous = history['reefy']['logical_volumes'].get(name)
                require(previous == old, 'persistent definition changed in history')
        else:
            require(seg['origin'] in lvs and segment(lvs[seg['origin']])['type'] == 'thin', 'unknown old origin')
            require(transaction >= seg['transaction_id'], 'snapshot creation moved backwards')
            require(transaction <= actual_transaction, 'future snapshot creation')
            # Snapshot provenance may be stale. Never revive its old identity.
            # Preserve the current device as an unrelated, read-only LV.
            renamed = 'recovered_unattributed_' + str(device_id)
            require(renamed not in recovered, 'recovery name collision')
            item = recovered.pop(name)
            raw_id = uuid.uuid4().hex
            item['id'] = '-'.join((raw_id[:6], raw_id[6:10], raw_id[10:14],
                                   raw_id[14:18], raw_id[18:22], raw_id[22:26], raw_id[26:]))
            item['status'] = ['READ', 'VISIBLE']
            item.pop('tags', None)
            item['flags'] = ['ACTIVATION_SKIP']
            item.pop('creation_time', None)
            item.pop('creation_host', None)
            item['segment1'].pop('origin')
            item['segment1']['transaction_id'] = transaction
            recovered[renamed] = item
            quarantined.append(renamed)
    new_pool = recovered['reefy_pool']['segment1']
    for key in list(new_pool):
        if re.fullmatch(r'message\d+', key):
            message = new_pool[key]
            require(set(message) <= {'create', 'delete'} and len(message) == 1,
                    'unsupported pending thin message')
            del new_pool[key]
    require(quarantined or actual_transaction == pool['transaction_id'],
            'transaction gap without identified snapshot replacement')
    new_pool['transaction_id'] = actual_transaction
    result['reefy']['seqno'] += 1
    return result, quarantined


def render(values, indent=0):
    lines = []
    for key, value in values.items():
        require(re.fullmatch(r'[A-Za-z_][A-Za-z_0-9.+-]*', key) is not None, 'invalid key')
        if isinstance(value, dict):
            lines += [' ' * indent + key + ' {', render(value, indent + 2), ' ' * indent + '}']
        else:
            lines.append(' ' * indent + key + ' = ' + json.dumps(value))
    return '\n'.join(lines) + '\n'
