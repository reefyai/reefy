"""Offline outer-VG recovery before storage activation.

Only the single-PV linear Reefy layout is supported. Original metadata is
saved on the boot ESP before pvck writes anything. Thin metadata is never
repaired or assigned a different transaction by this helper.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import zlib

from reefy.lvm_text import parse
from reefy.vg_recovery_plan import candidate, layout, render, require

MIB = 1024**2
MAX_PREFIX = 16 * MIB
MAX_THIN = 128 * MIB
MAX_BUNDLE = 64 * MIB
ESP = Path('/mnt/reefy')


def log(message):
    print('[reefy] VG recovery: ' + message, flush=True)


def run(args, timeout=120, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr, flush=True)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stdout[-4096:]}')
    return result


def crc(data):
    # LVM uses the standard reflected CRC32 with its own initial value and
    # no final XOR; zlib applies both initial/final XOR internally.
    return zlib.crc32(data, 0xf597a6cf ^ 0xffffffff) ^ 0xffffffff


def headers(prefix, device_size):
    require(len(prefix) >= 4608, 'short PV prefix')
    label = prefix[512:1024]
    require(label[:8] == b'LABELONE' and label[24:32] == b'LVM2 001', 'unsupported PV label')
    require(struct.unpack_from('<Q', label, 8)[0] == 1, 'unexpected label sector')
    require(crc(label[20:]) == struct.unpack_from('<I', label, 16)[0], 'PV label checksum')
    offset = struct.unpack_from('<I', label, 20)[0]
    require(32 <= offset <= 128, 'PV header offset')
    pvid = label[offset:offset + 32].decode('ascii')
    require(re.fullmatch('[A-Za-z0-9]{32}', pvid), 'PV identifier')
    require(struct.unpack_from('<Q', label, offset + 32)[0] == device_size, 'PV device size changed')
    pos = offset + 40
    areas = []
    for _ in range(2):
        entries = []
        while True:
            require(pos + 16 <= len(label), 'unterminated PV areas')
            start, size = struct.unpack_from('<QQ', label, pos)
            pos += 16
            if start == 0:
                require(size == 0, 'bad PV area terminator')
                break
            entries.append((start, size))
        areas.append(entries)
    require(len(areas[0]) == len(areas[1]) == 1, 'multiple PV data/metadata areas')
    data_start, data_size = areas[0][0]
    mda_start, mda_size = areas[1][0]
    require(mda_start == 4096 and 512 < mda_size <= MAX_PREFIX - mda_start,
            'unsupported metadata area')
    require(data_size == 0 and data_start >= mda_start + mda_size
            and data_start <= MAX_PREFIX and data_start % 512 == 0, 'unsupported data area')
    header = prefix[mda_start:mda_start + 512]
    require(crc(header[4:]) == struct.unpack_from('<I', header)[0], 'MDA header checksum')
    require(header[4:20] == b' LVM2 x[5A%r0N*>'
            and struct.unpack_from('<IQQ', header, 20) == (1, mda_start, mda_size), 'MDA geometry')
    record_offset, record_size, checksum, flags = struct.unpack_from('<QQII', header, 40)
    require(flags == 0 and 512 <= record_offset < mda_size and record_offset % 512 == 0
            and 0 < record_size <= mda_size - 512, 'invalid committed-record pointer')
    require(header[64:88] == bytes(24), 'multiple active MDA records')
    return dict(pvid=pvid, data_start=data_start, mda_start=mda_start,
                mda_size=mda_size, record_offset=record_offset,
                record_size=record_size, checksum=checksum)


def ring_read(prefix, info, offset, size):
    start, end = info['mda_start'], info['mda_start'] + info['mda_size']
    first = prefix[start + offset:min(start + offset + size, end)]
    return first + prefix[start + 512:start + 512 + size - len(first)]


def records_from_ring(prefix, info):
    current = ring_read(prefix, info, info['record_offset'], info['record_size'])
    require(crc(current) != info['checksum'], 'committed VG record has a valid checksum; refusing historical rollback')
    records = []
    for offset in range(512, info['mda_size'], 512):
        if prefix[info['mda_start'] + offset:info['mda_start'] + offset + 7] != b'reefy {':
            continue
        raw = ring_read(prefix, info, offset, min(MIB, info['mda_size'] - 512))
        try:
            text, terminator, _ = raw.partition(b'\0')
            if not terminator:
                continue
            record = parse(text.decode('ascii'))
            if 'reefy' in record:
                records.append(record)
        except (UnicodeError, ValueError):
            continue
    require(0 < len(records) <= 1024, 'no usable VG history or excessive records')
    # Two different surviving definitions at the same sequence are ambiguous.
    seen = {}
    for record in records:
        seq = record['reefy']['seqno']
        require(type(seq) is int and seq > 0, 'invalid sequence')
        require(seq not in seen or seen[seq] == record, 'conflicting VG sequence')
        seen[seq] = record
    return [seen[seq] for seq in sorted(seen)]


def idle(pv):
    st = os.stat(pv)
    require(stat.S_ISBLK(st.st_mode), 'PV is not a block device')
    holders = Path(f'/sys/dev/block/{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}/holders')
    require(holders.exists() and not list(holders.iterdir()), 'PV has active holders')


def physical(vg, name):
    seg = vg['logical_volumes'][name]['segment1']
    pv_name, extent = seg['stripes']
    return ((vg['physical_volumes'][pv_name]['pe_start'] + extent * vg['extent_size']),
            seg['extent_count'] * vg['extent_size'])


def xfs_geometry(device, sectors):
    result = run(['xfs_db', '-r', '-c', 'sb 0', '-c', 'p magicnum blocksize dblocks uuid', device])
    fields = dict(re.findall(r'^(magicnum|blocksize|dblocks|uuid) = (\S+)$', result.stdout, re.M))
    require(set(fields) == {'magicnum', 'blocksize', 'dblocks', 'uuid'}
            and fields['magicnum'] == '0x58465342' and not result.stderr.strip()
            and len(result.stdout.strip().splitlines()) == 4, 'unreadable or invalid XFS superblock')
    used = int(fields['blocksize']) * int(fields['dblocks'])
    require(int(fields['blocksize']) in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)
            and 0 < used <= sectors * 512, 'XFS exceeds historical LV size')
    return fields['uuid']


def validate_filesystems(pv, plan):
    """Use kernel read-only mappings without trusting a changed VG transaction.

    read_only is a dm-thin feature: no metadata commits, snapshot messages,
    discards or filesystem journal replay are permitted during this probe.
    """
    vg = plan['reefy']
    pool = vg['logical_volumes']['reefy_pool']['segment1']
    names = []
    base = f'reefy-vg-probe-{os.getpid()}'
    def create(suffix, table, readonly=True):
        name = base + '-' + suffix
        names.append(name)
        run(['dmsetup', 'create', name, *(['--readonly'] if readonly else []), '--table', table])
        return '/dev/mapper/' + name
    try:
        meta_offset, meta_size = physical(vg, pool['metadata'])
        data_offset, data_size = physical(vg, pool['pool'])
        meta = create('meta', f'0 {meta_size} linear {pv} {meta_offset}')
        # dm-thin requires a writable data-device handle even in read_only
        # mode. Exposed thin devices are read-only; no filesystem is mounted.
        data = create('data', f'0 {data_size} linear {pv} {data_offset}', readonly=False)
        thin_pool = create('pool', f'0 {data_size} thin-pool {meta} {data} {pool["chunk_size"]} 0 2 read_only ignore_discard', readonly=False)
        uuids = set()
        for index, (name, lv) in enumerate(vg['logical_volumes'].items()):
            seg = lv['segment1']
            sectors = seg['extent_count'] * vg['extent_size']
            if seg['type'] == 'thin':
                device = create(str(index), f'0 {sectors} thin {thin_pool} {seg["device_id"]}')
                status_fields = run(['dmsetup', 'status', '--noflush', device]).stdout.split()
                require(len(status_fields) == 5 and status_fields[2] == 'thin',
                        'invalid thin-device status')
                require(int(status_fields[3]) == 0 or int(status_fields[4]) < sectors,
                        'thin mappings exceed historical LV size')
            elif name == 'reefy_state':
                offset, sectors = physical(vg, name)
                device = create(str(index), f'0 {sectors} linear {pv} {offset}')
            else:
                continue
            fs_uuid = xfs_geometry(device, sectors)
            if not name.startswith('recovered_unattributed_'):
                require(fs_uuid not in uuids, 'duplicate persistent filesystem identity')
                uuids.add(fs_uuid)
        log('Read-only XFS geometry checks passed')
    finally:
        failures = []
        for name in reversed(names):
            if run(['dmsetup', 'remove', '--retry', name], check=False).returncode:
                failures.append(name)
        require(not failures, 'probe mappings could not be removed')


@contextlib.contextmanager
def writable_esp():
    result = run(['findmnt', '-rn', '-o', 'FSTYPE,OPTIONS,TARGET', str(ESP)])
    fs_type, options, target = result.stdout.strip().split()
    require(fs_type == 'vfat' and target == str(ESP), 'boot ESP is unavailable')
    readonly = 'ro' in options.split(',')
    if readonly:
        run(['mount', '-o', 'remount,rw', str(ESP)])
    try:
        yield
    finally:
        if readonly:
            run(['mount', '-o', 'remount,ro', str(ESP)])


def preserve(work):
    """One bounded, durable evidence bundle; never rotate away originals."""
    directory = ESP / 'recovery'
    destination = directory / 'vg-metadata.tar.gz'
    marker = directory / 'vg-metadata.attempted'
    with writable_esp():
        directory.mkdir(exist_ok=True)
        require(not destination.exists() and not marker.exists(), 'previous VG recovery evidence exists; operator review required')
        require(shutil.disk_usage(directory).free > MAX_BUNDLE + 8 * MIB, 'insufficient ESP space for recovery evidence')
        temporary = directory / 'vg-metadata.partial'
        require(not temporary.exists(), 'incomplete previous evidence bundle')
        try:
            with open(temporary, 'xb') as output:
                class Bounded:
                    def write(self, data):
                        require(output.tell() + len(data) <= MAX_BUNDLE, 'evidence exceeds 64 MiB limit')
                        return output.write(data)
                    def tell(self):
                        return output.tell()
                    def flush(self):
                        output.flush()
                with tarfile.open(fileobj=Bounded(), mode='w:gz') as archive:
                    for path in sorted(work.iterdir()):
                        archive.add(path, arcname=path.name)
                output.flush()
                os.fsync(output.fileno())
            os.rename(temporary, destination)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            with open(marker, 'x') as stream:
                stream.write(digest + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception:
            # Retain partial evidence and refuse subsequent attempts.
            raise
    log('Original metadata saved durably on boot ESP (bounded to 64 MiB)')


def recover(pvs):
    require(len(pvs) == 1, 'automatic recovery supports exactly one PV')
    pv = pvs[0]
    require(re.fullmatch('/dev/mapper/reefy-[A-Za-z0-9_-]+', pv), 'unexpected PV path')
    idle(pv)
    require(run(['vgs', 'reefy'], check=False).returncode != 0, 'VG already readable')
    size = int(run(['blockdev', '--getsize64', pv]).stdout)
    with open(pv, 'rb', buffering=0) as stream:
        initial = stream.read(4608)
        info = headers(initial, size)
        stream.seek(0)
        prefix = stream.read(info['data_start'])
    require(len(prefix) == info['data_start'], 'short PV read')
    records = records_from_ring(prefix, info)
    latest = records[-1]['reefy']
    layout(records[-1])
    definition = next(iter(latest['physical_volumes'].values()))
    require(definition['id'].replace('-', '') == info['pvid']
            and definition['pe_start'] * 512 == info['data_start']
            and definition['dev_size'] * 512 == size
            and (definition['pe_start'] + definition['pe_count'] * latest['extent_size']) * 512 <= size,
            'VG and PV header disagree')
    meta_name = latest['logical_volumes']['reefy_pool']['segment1']['metadata']
    offset, sectors = physical(latest, meta_name)
    require(0 < sectors * 512 <= MAX_THIN and (offset + sectors) * 512 <= size, 'thin metadata bounds')
    with tempfile.TemporaryDirectory(prefix='reefy-vg-', dir='/run') as temp:
        work = Path(temp)
        (work / 'pv-prefix.bin').write_bytes(prefix)
        with open(pv, 'rb', buffering=0) as stream:
            stream.seek(offset * 512)
            thin = stream.read(sectors * 512)
        require(len(thin) == sectors * 512, 'short thin metadata read')
        (work / 'thin.bin').write_bytes(thin)
        run(['/usr/sbin/thin_check', str(work / 'thin.bin')])
        xml = run(['/usr/sbin/thin_dump', '--skip-mappings', str(work / 'thin.bin')]).stdout
        plan, snapshots = candidate(records, xml)
        validate_filesystems(pv, plan)
        # vgcfgbackup-style wrapper avoids pvck treating a backup as raw text.
        text = '# Generated by LVM2\n' + render(plan)
        (work / 'candidate.vg').write_text(text)
        (work / 'report.json').write_text(json.dumps({'records': len(records),
            'source_sequence': latest['seqno'], 'quarantined': snapshots,
            'pv_prefix_sha256': hashlib.sha256(prefix).hexdigest(),
            'thin_sha256': hashlib.sha256(thin).hexdigest()}, indent=2))
        preserve(work)
        idle(pv)
        with open(pv, 'rb', buffering=0) as stream:
            require(stream.read(len(prefix)) == prefix, 'PV changed after validation')
            stream.seek(offset * 512)
            require(stream.read(len(thin)) == thin, 'thin metadata changed after validation')
        run(['pvck', '--yes', '--repairtype', 'metadata', '--file', str(work / 'candidate.vg'), pv])
        run(['vgs', 'reefy'])
        log(f'VG metadata reconstructed; {len(snapshots)} snapshot(s) quarantined read-only')


def main():
    os.umask(0o077)
    with open('/run/reefy-vg-recovery.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            recover(sys.argv[1:])
        except Exception as error:
            log(f'REFUSED: {error}')
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
