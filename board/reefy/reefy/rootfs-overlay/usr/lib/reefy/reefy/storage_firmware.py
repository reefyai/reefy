"""Reject accidental rollback to firmware without the active quota protocol.

Inspect the existing immutable compatibility manifest inside the target UKI.
Version ordering cannot prove support: a later build of an older branch may
still lack it. No new firmware or command-payload fields are needed.
"""
import contextlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile

from reefy.compatibility import validate_manifest
from reefy.storage_pressure import PressureError
from reefy.storage_quota import Registry, command


def squashfs_extent(stream):
    """Locate the uncompressed newc rootfs payload without extracting it."""
    size = os.fstat(stream.fileno()).st_size

    def read(offset, length):
        if offset < 0 or length < 0 or offset + length > size:
            raise PressureError('firmware payload exceeds its file boundary')
        stream.seek(offset)
        value = stream.read(length)
        if len(value) != length:
            raise PressureError('truncated firmware payload')
        return value

    if read(0, 2) != b'MZ':
        raise PressureError('firmware is not a PE image')
    pe = struct.unpack('<I', read(60, 4))[0]
    header = read(pe, 24)
    if header[:4] != b'PE\0\0':
        raise PressureError('invalid firmware PE signature')
    sections = struct.unpack_from('<H', header, 6)[0]
    optional_size = struct.unpack_from('<H', header, 20)[0]
    if not 0 < sections <= 96:
        raise PressureError('invalid firmware section count')
    matches = []
    for index in range(sections):
        row = read(pe + 24 + optional_size + 40 * index, 40)
        if row[:8].rstrip(b'\0') == b'.initrd':
            length, offset = struct.unpack_from('<II', row, 16)
            if not length or offset + length > size:
                raise PressureError('invalid initrd extent')
            matches.append((offset, length))
    if len(matches) != 1:
        raise PressureError('firmware requires exactly one initrd')
    start, length = matches[0]
    position, end = start, start + length
    for _ in range(100000):
        if position + 110 > end:
            break
        header = read(position, 110)
        if header[:6] != b'070701':
            raise PressureError('unsupported firmware initrd format')
        try:
            fields = [int(header[6 + i * 8:14 + i * 8], 16) for i in range(13)]
        except ValueError as error:
            raise PressureError('invalid initrd header') from error
        file_size, name_size = fields[6], fields[11]
        if not 0 < name_size <= 4096 or position + 110 + name_size > end:
            raise PressureError('invalid initrd filename')
        name = read(position + 110, name_size)
        if not name.endswith(b'\0'):
            raise PressureError('unterminated initrd filename')
        # newc padding is relative to the start of the archive.
        data = start + ((position - start + 110 + name_size + 3) & ~3)
        if data + file_size > end:
            raise PressureError('initrd entry exceeds its section')
        if name[:-1] in (b'rootfs.squashfs', b'./rootfs.squashfs'):
            if fields[1] & 0o170000 != 0o100000 or file_size < 96:
                raise PressureError('root filesystem is not a regular SquashFS payload')
            if read(data, 4) != b'hsqs':
                raise PressureError('invalid root filesystem magic')
            return data, file_size
        if name[:-1] == b'TRAILER!!!':
            break
        position = start + ((data - start + file_size + 3) & ~3)
    raise PressureError('firmware root filesystem is missing')


@contextlib.contextmanager
def readonly_mount(source, *, options='ro,nodev,nosuid,noexec', filesystem=None):
    directory = tempfile.mkdtemp(prefix='reefy-firmware-check-', dir='/run')
    mounted = False
    try:
        args = ['mount', '-o', options]
        if filesystem:
            args += ['-t', filesystem]
        command(args + [str(source), directory], timeout=30)
        mounted = True
        yield Path(directory)
    finally:
        if mounted:
            command(['umount', directory], timeout=30)
        # Never recursively remove a directory that might still be mounted.
        os.rmdir(directory)


def require_compatible_image(path):
    registry = Registry()
    if not (registry.data.get('active') or registry.data.get('activation_pending')):
        return
    with open(path, 'rb') as stream:
        offset, size = squashfs_extent(stream)
    # mount's loop device is autocleared on unmount; no payload is copied into
    # the pressured pool. The target image is never executed or written.
    with readonly_mount(path, filesystem='squashfs',
                        options=f'loop,ro,nodev,nosuid,noexec,offset={offset},sizelimit={size}') as root:
        with (root / 'usr/share/reefy/compatibility.json').open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise PressureError('firmware compatibility manifest is too large')
        manifest = validate_manifest(json.loads(raw))
        revision = manifest['protocols'].get('desired_state', {}).get('features', {}).get('storage_pressure_quotas')
        if revision != 1:
            raise PressureError('target firmware does not support the active storage quota policy')


def main():
    try:
        if sys.argv[1] == 'slot':
            if not (Registry().data.get('active') or Registry().data.get('activation_pending')):
                return
            with readonly_mount(sys.argv[2]) as root:
                require_compatible_image(root / 'EFI/Boot/bootx64.efi')
        else:
            require_compatible_image(sys.argv[1])
    except Exception as error:
        print(f'Firmware storage compatibility check failed: {error}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
