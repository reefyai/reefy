"""Best-effort background VG metadata redundancy maintenance with stock vgck.

Leave activation and mounting unchanged. Never restore an exported transaction
or reconstruct historical metadata. Legacy one-area PVs remain unchanged.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import time

from reefy.lvm_text import parse
from reefy.vg_recovery import crc, run
from reefy.vg_recovery_plan import Refused, layout, require

MIB = 1024 ** 2
MAX_AREA = 16 * MIB


def log(message):
    print('[reefy] PV redundancy: ' + message, flush=True)


def read_pv(pv, size):
    """Read bounded current records; validate headers independently of LVM."""
    with open(pv, 'rb', buffering=0) as stream:
        stream.seek(512)
        label = stream.read(512)
        require(len(label) == 512 and label[:8] == b'LABELONE'
                and label[24:32] == b'LVM2 001', 'unsupported PV label')
        require(struct.unpack_from('<Q', label, 8)[0] == 1, 'label sector')
        require(crc(label[20:]) == struct.unpack_from('<I', label, 16)[0], 'label checksum')
        offset = struct.unpack_from('<I', label, 20)[0]
        require(32 <= offset <= 128, 'PV header offset')
        pvid = label[offset:offset + 32].decode('ascii')
        require(re.fullmatch('[A-Za-z0-9]{32}', pvid), 'PV identifier')
        require(struct.unpack_from('<Q', label, offset + 32)[0] == size, 'PV size changed')
        position = offset + 40
        locations = []
        for _ in range(2):
            entries = []
            while True:
                require(position + 16 <= 512, 'unterminated PV areas')
                start, length = struct.unpack_from('<QQ', label, position)
                position += 16
                if not start:
                    require(length == 0, 'PV area terminator')
                    break
                entries.append((start, length))
            locations.append(entries)
        require(len(locations[0]) == 1, 'multiple data areas')
        data_start, data_size = locations[0][0]
        require(data_size == 0 and data_start % 512 == 0, 'data geometry')
        if len(locations[1]) <= 1:
            return None  # Preserve legacy recovery semantics, including bad text.
        require(len(locations[1]) == 2, 'unsupported metadata area count')
        (front, front_size), (rear, rear_size) = locations[1]
        require(front == 4096 and front + front_size <= data_start < rear
                and rear + rear_size == size, 'metadata/data overlap or changed bounds')
        areas = []
        for start, length in locations[1]:
            require(start % 512 == 0 and length % 512 == 0
                    and 512 < length <= MAX_AREA, 'metadata area bounds')
            stream.seek(start)
            raw = stream.read(length)
            require(len(raw) == length, 'short metadata read')
            header = raw[:512]
            require(crc(header[4:]) == struct.unpack_from('<I', header)[0], 'MDA header checksum')
            require(header[4:20] == b' LVM2 x[5A%r0N*>'
                    and struct.unpack_from('<IQQ', header, 20) == (1, start, length), 'MDA geometry')
            off, count, checksum, flags = struct.unpack_from('<QQII', header, 40)
            require(flags == 0 and 512 <= off < length and off % 512 == 0
                    and 0 < count <= length - 512, 'committed metadata pointer')
            require(header[64:88] == bytes(24), 'pending or additional metadata record')
            first = raw[off:min(off + count, length)]
            text = first + raw[512:512 + count - len(first)]
            areas.append(dict(start=start, size=length, raw=raw, text=text,
                              valid=crc(text) == checksum))
    return dict(label=label, pvid=pvid, data_start=data_start, areas=areas)


def validate_source(snapshot, size):
    areas = snapshot['areas']
    valid = [index for index, area in enumerate(areas) if area['valid']]
    require(len(valid) == 1, 'exactly one trustworthy current record required')
    source = valid[0]
    text = areas[source]['text']
    record = parse(text.rstrip(b'\0').decode('ascii'))
    require([key for key, value in record.items() if isinstance(value, dict)] == ['reefy'],
            'foreign or ambiguous VG')
    vg = record['reefy']
    require(type(vg.get('seqno')) is int and vg['seqno'] > 0, 'invalid sequence')
    layout(record)  # Constrained modern single-PV layout, no transaction edits.
    definition = next(iter(vg['physical_volumes'].values()))
    require(definition['id'].replace('-', '') == snapshot['pvid'], 'PV identity mismatch')
    require(definition['dev_size'] * 512 == size
            and definition['pe_start'] * 512 == snapshot['data_start'], 'PV geometry mismatch')
    end = (definition['pe_start'] + definition['pe_count'] * vg['extent_size']) * 512
    require(end <= areas[1]['start'], 'physical extents overlap rear metadata')
    return source


# These options apply only to this maintenance process, never global LVM policy.
NONBLOCK = 'global { wait_for_locks=0 prioritise_write_locks=0 }'
STATUS = Path('/mnt/reefy-data/state/pv-redundancy/last-result.json')
RUN_STATUS = Path('/run/reefy/pv-redundancy.json')
MAX_ATTEMPTS = 3
BACKOFF = (1, 2)


def native(args):
    return run([args[0], '--config', NONBLOCK, *args[1:]], check=False)


def discover():
    result = native(['pvs', '--reportformat', 'json', '--units', 'b',
                     '--nosuffix', '-o', 'pv_name,vg_name'])
    if result.returncode:
        raise RuntimeError('PV discovery unavailable or busy: ' + result.stderr[-2048:])
    rows = json.loads(result.stdout)['report'][0]['pv']
    return [row['pv_name'].strip() for row in rows if row['vg_name'].strip() == 'reefy']


def inspect(pvs):
    damaged = []
    for pv in pvs:
        require(re.fullmatch('/dev/mapper/reefy-[A-Za-z0-9_-]+', pv), 'unexpected PV path')
        size = int(run(['blockdev', '--getsize64', pv]).stdout)
        snapshot = read_pv(pv, size)
        if snapshot is None:
            continue  # No automatic reconstruction of legacy one-area metadata.
        areas = snapshot['areas']
        if all(area['valid'] for area in areas):
            require(areas[0]['text'] == areas[1]['text'], 'current records differ; defer concurrent or ambiguous change')
            continue
        validate_source(snapshot, size)
        damaged.append((pv, snapshot))
    require(not damaged or len(pvs) == len(damaged) == 1,
            'damaged multi-PV storage requires review')
    return damaged


def observe(damaged):
    # Save hashes and validity, not metadata text or customer data.
    return [{'pv': pv, 'pvid': snapshot['pvid'],
             'valid': [area['valid'] for area in snapshot['areas']],
             'text_sha256': [hashlib.sha256(area['text']).hexdigest()
                             for area in snapshot['areas']]}
            for pv, snapshot in damaged]


def recover():
    result = {'outcome': 'degraded', 'attempts': [], 'writes_requested': 0}
    observed_damage = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        entry = {'attempt': attempt}
        result['attempts'].append(entry)
        try:
            pvs = discover()
            damaged = inspect(pvs)
        except Refused as error:
            entry['error'] = str(error)
            result['outcome'] = 'deferred'
            break  # Never retry ambiguous metadata, bad headers, or unsupported layouts.
        except Exception as error:
            entry['error'] = str(error)
            # Busy discovery is best effort; no metadata writes were requested.
            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF[attempt - 1])
                continue
            result['outcome'] = 'deferred'
            break
        if not damaged:
            result['outcome'] = 'repaired' if observed_damage else 'healthy'
            break
        observed_damage = True
        entry['before'] = observe(damaged)
        result['writes_requested'] += 1
        repair = native(['vgck', '--updatemetadata', 'reefy'])
        entry.update(returncode=repair.returncode,
                     output=(repair.stdout + repair.stderr)[-16384:])
        # Verification is independent of vgck's exit status. A subsequent scan
        # must rediscover the current VG; never replay a previously exported record.
        try:
            current_pvs = discover()
            require(current_pvs == pvs, 'PV set changed during repair')
            for pv, original in damaged:
                size = int(run(['blockdev', '--getsize64', pv]).stdout)
                current = read_pv(pv, size)
                require(current is not None and current['label'] == original['label'],
                        'PV identity or geometry changed during repair')
            remaining = inspect(current_pvs)
            entry['after'] = observe(remaining)
            entry['verified'] = not remaining
            if not remaining:
                result['outcome'] = 'repaired'
                break
        except Exception as error:
            entry['verification_error'] = str(error)
            result['outcome'] = 'deferred'
            break
        if repair.returncode:
            # Do not repeatedly write through operational or I/O failures.
            result['outcome'] = 'deferred'
            break
        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF[attempt - 1])
    return result


def save_result(result):
    # /run is always available; persistent state is used only when real storage
    # is already mounted. Reporting never turns a usable boot into a failure.
    for target in (RUN_STATUS, STATUS):
        try:
            if target == STATUS and not os.path.ismount('/mnt/reefy-data'):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(result, indent=2) + '\n')
            temporary.replace(target)
        except OSError as error:
            log('Could not retain diagnostic result: ' + str(error))


def main():
    os.umask(0o077)
    with open('/run/reefy-vg-recovery.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log('Another recovery job owns the lock; skipping')
            return 0
        try:
            result = recover()
        except Exception as error:
            result = {'outcome': 'deferred', 'error': str(error), 'attempts': []}
        save_result(result)
        if result['outcome'] == 'repaired':
            log('Current VG metadata redundancy restored and independently verified')
        else:
            log('Background check finished: ' + result['outcome'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
