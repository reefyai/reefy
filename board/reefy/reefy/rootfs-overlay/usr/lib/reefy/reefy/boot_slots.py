"""Create the missing B slot without modifying the booted A filesystem."""
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid

from reefy.shared import _part_dev
from reefy.storage import _boot_gpt_identity, _repair_boot_gpt

ESP_TYPE = uuid.UUID('c12a7328-f81f-11d2-ba4b-00a0c93ec93b').bytes_le
PENDING_TYPE = uuid.UUID('e3c9e316-0b5c-4db8-817d-f92df00215ae').bytes_le
PENDING_NAME = 'reefy-b-initializing'
MIB = 1024 * 1024


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=120).stdout.strip()


def layout(disk):
    """Use the existing CRC/identity validation before interpreting geometry."""
    _, table = _boot_gpt_identity(disk)
    with open(disk, 'rb', buffering=0) as stream:
        sector = 512
        if stat.S_ISBLK(os.fstat(stream.fileno()).st_mode):
            sector = struct.unpack('I', fcntl.ioctl(
                stream.fileno(), 0x1268, struct.pack('I', 0)))[0]
        size = stream.seek(0, os.SEEK_END)
    partitions = {}
    for index in range(0, len(table), 128):
        entry = table[index:index + 128]
        if entry[:16] == bytes(16):
            continue
        start, end = struct.unpack_from('<QQ', entry, 32)
        if start < 34 or end < start or (end + 1) * sector > size:
            raise RuntimeError('Invalid partition bounds; preserving disk')
        partitions[index // 128 + 1] = {
            'start': start, 'end': end, 'type': entry[:16],
            'name': entry[56:128].decode('utf-16-le').rstrip('\0'),
        }
    ordered = sorted(partitions.values(), key=lambda p: p['start'])
    if any(a['end'] >= b['start'] for a, b in zip(ordered, ordered[1:])):
        raise RuntimeError('Overlapping partitions; preserving disk')
    return sector, size // sector - (len(table) + sector - 1) // sector - 2, partitions


def align(value, sector):
    unit = MIB // sector
    return (value + unit - 1) // unit * unit


def next_partition_start(disk, numbers):
    sector, last, partitions = layout(disk)
    if any(number not in partitions for number in numbers):
        raise RuntimeError('Required boot partitions are missing')
    if any(partitions[n]['type'] != ESP_TYPE or partitions[n]['name'] not in
           (('reefy-a', 'sbnb-a') if n == 1 else ('reefy-b', 'sbnb-b'))
           for n in (1, 2) if n in numbers):
        raise RuntimeError('Boot slots are not verified; refusing storage allocation')
    start = align(max(partitions[n]['end'] for n in numbers) + 1, sector)
    if start > last:
        raise RuntimeError('No free space after boot partitions')
    return start


def manifest(root):
    result = {}
    for path in sorted(Path(root).rglob('*')):
        if path.is_symlink():
            raise RuntimeError('Unexpected symlink in boot filesystem')
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(MIB), b''):
                    digest.update(block)
            result[str(path.relative_to(root))] = (path.stat().st_size, digest.hexdigest())
    if 'EFI/Boot/bootx64.efi' not in result:
        raise RuntimeError('Boot firmware is missing')
    return result


def ensure_slots(disk, source='/mnt/reefy'):
    sector, last, partitions = layout(disk)
    a = partitions.get(1)
    if not a or a['name'] not in ('reefy-a', 'sbnb-a') or a['type'] != ESP_TYPE:
        raise RuntimeError('Cannot identify slot A; preserving disk')
    b = partitions.get(2)
    if b and b['name'] in ('reefy-b', 'sbnb-b') and b['type'] == ESP_TYPE:
        print('[boot-slots] Existing B preserved', flush=True)
        return
    start = align(a['end'] + 1, sector)
    end = start + a['end'] - a['start']
    if end > last or any(
            n not in (1, 2) and p['start'] <= end and p['end'] >= start
            for n, p in partitions.items()):
        raise RuntimeError('No safe space for slot B; preserving disk')
    if b and not (b['name'] == PENDING_NAME and b['type'] == PENDING_TYPE
                  and b['start'] == start and b['end'] == end):
        raise RuntimeError('Conflicting partition B; preserving disk')
    expected = manifest(source)
    if sum(v[0] for v in expected.values()) + 16 * MIB > (end - start + 1) * sector:
        raise RuntimeError('Boot payload does not fit slot B')
    _repair_boot_gpt(disk)
    if not b:
        run(['sgdisk', f'--new=2:{start}:{end}', '--typecode=2:0c01',
             f'--change-name=2:{PENDING_NAME}', disk])
        run(['partprobe', disk])
    target = _part_dev(disk, 2)
    for _ in range(50):
        if os.path.exists(target):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError('Slot B device did not appear')
    run(['mkfs.vfat', '-F', '32', '-n', 'reefy-b', target])
    with tempfile.TemporaryDirectory(prefix='reefy-slot-b-') as destination:
        mounted = False
        try:
            run(['mount', target, destination])
            mounted = True
            for item in Path(source).iterdir():
                dest = Path(destination) / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, copy_function=shutil.copyfile)
                else:
                    shutil.copyfile(item, dest)
            os.sync()
            if manifest(source) != expected or manifest(destination) != expected:
                raise RuntimeError('Slot B copy verification failed')
            run(['umount', destination])
            mounted = False
            with open(target, 'rb', buffering=0) as stream:
                os.fsync(stream.fileno())
            run(['mount', '-o', 'ro', target, destination])
            mounted = True
            if manifest(destination) != expected:
                raise RuntimeError('Durable slot B verification failed')
        finally:
            if mounted:
                run(['umount', destination])
    run(['sgdisk', '--typecode=2:ef00', '--change-name=2:reefy-b', disk])
    run(['partprobe', disk])
    print('[boot-slots] Slot B populated and verified', flush=True)


def check_fit(payload, destination):
    size = int(run(['blockdev', '--getsize64', destination]))
    if run(['lsblk', '-ndo', 'PARTTYPE', destination]).lower() != str(uuid.UUID(bytes_le=ESP_TYPE)):
        raise RuntimeError('Destination is not a verified boot ESP')
    if Path(payload).stat().st_size + 16 * MIB > size:
        raise RuntimeError('Firmware exceeds destination slot capacity')


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--check-fit':
        check_fit(sys.argv[2], sys.argv[3])
        return
    source = run(['findmnt', '-rn', '-o', 'SOURCE', '/mnt/reefy'])
    parent = run(['lsblk', '-ndo', 'PKNAME', source])
    if not parent or '/' in parent:
        raise RuntimeError('Cannot identify boot disk')
    disk = '/dev/' + parent
    _, _, partitions = layout(disk)
    if 2 not in partitions or partitions[2]['name'] == PENDING_NAME:
        if os.path.realpath(source) != os.path.realpath(_part_dev(disk, 1)):
            raise RuntimeError('Missing B initialization requires booted A')
    with open('/run/reefy-boot-slots.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ensure_slots(disk)


if __name__ == '__main__':
    main()
