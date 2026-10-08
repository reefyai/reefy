#!/usr/bin/env python3
"""Capture pre-strip system ELF files and verify a matching shipped sysroot."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def elf(path):
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:6] != b'\x7fELF\x02\x01':
            return None
        if struct.unpack_from('<H', header, 16)[0] not in (2, 3) or struct.unpack_from('<H', header, 18)[0] != 62:
            return None
        offset = struct.unpack_from('<Q', header, 40)[0]
        width, count, names_index = struct.unpack_from('<HHH', header, 58)
        if width != 64 or not count or names_index >= count:
            raise RuntimeError('unsupported runtime ELF section table')
        stream.seek(offset)
        table = [struct.unpack('<IIQQQQIIQQ', stream.read(64)) for _ in range(count)]
        stream.seek(table[names_index][4])
        names = stream.read(table[names_index][5])
        sections = {}
        for entry in table:
            name = names[entry[0]:].split(b'\0', 1)[0].decode()
            # SHT_NOBITS has no bytes in the file.
            if entry[1] == 8:
                continue
            stream.seek(entry[4])
            sections[name] = (entry[3], stream.read(entry[5]))
    identity = None
    for section in ('.note.gnu.build-id', '.note.go.buildid'):
        if section not in sections:
            continue
        data = sections[section][1]
        pos = 0
        while pos + 12 <= len(data):
            namesz, size, kind = struct.unpack_from('<III', data, pos)
            owner = data[pos + 12:pos + 12 + namesz].rstrip(b'\0')
            start = pos + 12 + ((namesz + 3) & ~3)
            value = data[start:start + size]
            if owner == b'GNU' and kind == 3:
                identity = ('gnu', value.hex())
            elif owner == b'Go' and kind == 4 and identity is None:
                identity = ('go', value.decode().rstrip('\0'))
            pos = start + ((size + 3) & ~3)
    if identity is None:
        raise RuntimeError(f'runtime ELF lacks GNU/Go build identity: {path}')
    code = hashlib.sha256()
    for name in ('.text', '.init', '.fini', '.plt', '.rodata'):
        if name in sections:
            address, data = sections[name]
            code.update(name.encode() + struct.pack('<Q', address) + data)
    return {'identity_type': identity[0], 'build_id': identity[1],
            'dwarf': '.debug_info' in sections or '.zdebug_info' in sections,
            'code_sha256': code.hexdigest()}


def key(info):
    return hashlib.sha256((info['identity_type'] + ':' + info['build_id']).encode()).hexdigest()


# Explicitly reviewed coverage gaps, not a basename/glob exclusion. Build IDs
# remain mandatory. Never use this list to accept a mismatched cached original.
KNOWN_SYMBOL_GAPS = {
    'usr/lib/sa/sadc': 'sysstat links with -s by default',
    'usr/bin/cifsiostat': 'sysstat links with -s by default',
    'usr/bin/iostat': 'sysstat links with -s by default',
    'usr/bin/mpstat': 'sysstat links with -s by default',
    'usr/bin/pidstat': 'sysstat links with -s by default',
    'usr/bin/sar': 'sysstat links with -s by default',
    'usr/bin/sadf': 'sysstat links with -s by default',
    'usr/bin/tapestat': 'sysstat links with -s by default',
    'usr/bin/mgmt': 'Reefy Go package links with -s -w',
    'usr/bin/cpupower': 'upstream production build strips before installation',
    'usr/bin/iwconfig': 'upstream multicall link hardcodes -Wl,-s',
    'usr/bin/borg': 'upstream prebuilt standalone executable has no DWARF',
}


def symbol_gap(relative, info):
    return KNOWN_SYMBOL_GAPS.get(str(relative)) if not info['dwarf'] else None


def native_files(root):
    for directory, _, names in os.walk(root, followlinks=False):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            info = elf(path)
            if info:
                yield path, info


def runtime_roots(output):
    yield output / 'target', Path('.')
    for name in ('nvidia', 'intel', 'amd'):
        toolkit = output / 'reefy-artifacts' / name / 'toolkit'
        if toolkit.exists():
            yield toolkit, Path('providers') / name / 'toolkit'


def installed_files(output):
    for root, prefix in runtime_roots(output):
        for path, info in native_files(root):
            yield path, info, prefix / path.relative_to(root)


def capture(output):
    cache = output / 'reefy-userspace-symbols'
    cache.mkdir(exist_ok=True)
    for path, info, relative in installed_files(output):
        saved = cache / key(info)
        if info['dwarf']:
            temporary = saved.with_suffix('.tmp')
            shutil.copy2(path, temporary)
            os.replace(temporary, saved)
        elif not saved.is_file():
            reason = symbol_gap(relative, info)
            if reason:
                print(f'documented symbol gap: {relative}: {reason}')
                continue
            # Unexpected gaps still fail; do not invent symbols.
            raise RuntimeError(f'no DWARF for installed system ELF: {relative}')
        original = elf(saved)
        if not original['dwarf'] or any(original[k] != info[k] for k in ('build_id', 'identity_type', 'code_sha256')):
            raise RuntimeError('pre-strip symbol cache does not match runtime code')
        if relative.parts[0] == 'providers':
            strip = output / 'host/bin/x86_64-buildroot-linux-gnu-strip'
            subprocess.run([str(strip), '--strip-unneeded', str(path)], check=True)


def archive(output, destination):
    target = output / 'target'
    cache = output / 'reefy-userspace-symbols'
    with tempfile.TemporaryDirectory(prefix='reefy-userspace-debug-') as directory:
        root = Path(directory)
        records = []
        gaps = []
        for path, info, relative in installed_files(output):
            original = cache / key(info)
            if not original.is_file():
                reason = symbol_gap(relative, info)
                if not reason:
                    raise RuntimeError(f'missing system debug original: {relative}')
                shipped = root / 'sysroot' / relative
                shipped.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, shipped)
                gaps.append((str(relative), reason, info['identity_type'],
                             info['build_id'], sha(path)))
                continue
            debug = elf(original)
            if not debug['dwarf'] or any(info[k] != debug[k] for k in ('build_id', 'identity_type', 'code_sha256')):
                raise RuntimeError('shipped system ELF mismatches archived symbols')
            for source, name in [(path, root / 'sysroot' / relative),
                                 (original, root / 'unstripped' / relative)]:
                name.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, name)
            if info['identity_type'] == 'gnu':
                build_id = info['build_id']
                link = root / 'debug/.build-id' / build_id[:2] / (build_id[2:] + '.debug')
                link.parent.mkdir(parents=True, exist_ok=True)
                if not link.exists():
                    link.symlink_to(os.path.relpath(root / 'unstripped' / relative, link.parent))
            records.append({**info, 'dwarf': debug['dwarf'], 'shipped_dwarf': info['dwarf'], 'path': str(relative), 'shipped_sha256': sha(path),
                            'unstripped_sha256': sha(original)})
        if not records:
            raise RuntimeError('empty system ELF debug inventory')
        # Human-readable coverage report is hashed with the bundle. The existing
        # metadata ELF inventory/index count continues to describe verified
        # symbol originals; unresolved runtime binaries are listed separately.
        report = ['Documented missing DWARF coverage',
                  'No unstripped originals or source-level debugging promised for these files.',
                  f'Verified symbol originals: {len(records)}',
                  f'Known unresolved binaries: {len(gaps)}', '']
        for path, reason, kind, identity, digest in sorted(gaps):
            report.extend([f'Path: {path}', f'Reason: {reason}',
                           f'Identity: {kind}:{identity}', f'Shipped SHA256: {digest}', ''])
        (root / 'SYMBOL-GAPS.txt').write_text('\n'.join(report) + '\n')
        # Preserve runtime aliases without absolute symlinks escaping the sysroot.
        for directory, names, files in os.walk(target, followlinks=False):
            for name in names + files:
                source = Path(directory) / name
                if not source.is_symlink():
                    continue
                relative = source.relative_to(target)
                value = os.readlink(source)
                if value.startswith('/'):
                    value = os.path.relpath(root / 'sysroot' / value.lstrip('/'),
                                            (root / 'sysroot' / relative).parent)
                resolved = (source.parent / os.readlink(source)).resolve() if not os.readlink(source).startswith('/') else target / os.readlink(source).lstrip('/')
                if not resolved.is_relative_to(target.resolve()):
                    continue
                for tree in ('sysroot', 'unstripped'):
                    link = root / tree / relative
                    link.parent.mkdir(parents=True, exist_ok=True)
                    if not link.exists() and not link.is_symlink():
                        link.symlink_to(value)
        # Only source code from target packages is included, never arbitrary
        # build trees, signing keys, config files, env dumps or host packages.
        source_suffixes = {'.c', '.h', '.cc', '.cpp', '.hpp', '.rs', '.go'}
        packages = []
        for package in (output / 'build').iterdir():
            if not package.is_dir() or package.name.startswith(('host-', 'linux-')) or not ((package / '.stamp_target_installed').exists() or (package.name.startswith('nvidia-container-toolkit-') and (package / '.stamp_built').exists())):
                continue
            packages.append(package.name)
            for source in package.rglob('*'):
                if source.is_symlink() or not source.is_file() or (source.suffix not in source_suffixes and source.name != 'python-gdb.py'):
                    continue
                name = root / 'sources' / package.relative_to(output) / source.relative_to(package)
                name.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, name)
        for source in (target / 'usr/lib/reefy').rglob('*.py'):
            if source.is_file() and not source.is_symlink():
                name = root / 'scripts' / source.relative_to(target)
                name.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, name)
        shutil.copy2(output / '.config', root / 'buildroot.config')
        shutil.copy2(target / 'usr/lib/os-release', root / 'os-release')
        metadata = {'elfs': records, 'packages': sorted(packages),
                    'reefy_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                    'buildroot_commit': subprocess.check_output(['git', 'ls-tree', 'HEAD', 'buildroot'], text=True).strip(),
                    'source_path_prefix': str(output)}
        (root / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
        paths = sorted(p for p in root.rglob('*') if p.is_file() and not p.is_symlink())
        (root / 'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.relative_to(root)}\n' for p in paths))
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['tar', '-I', 'pigz -p 8', '-cf', str(destination), '-C', str(root), '.'], check=True)
    release = dict(line.split('=', 1) for line in (target / 'usr/lib/os-release').read_text().splitlines() if '=' in line)
    index = {'image_version': release['IMAGE_VERSION'].strip(chr(34)), 'reefy_build_id': release['REEFY_BUILD_ID'].strip(chr(34)), 'archive_sha256': sha(destination), 'archive_bytes': destination.stat().st_size, 'system_elf_count': len(records)}
    destination.with_name('reefy-userspace-debug.json').write_text(json.dumps(index) + '\n')
    print(json.dumps(index))


def provider_ready(output):
    cache = output / 'reefy-userspace-symbols'
    toolkit = output / 'reefy-artifacts/nvidia/toolkit'
    if not toolkit.is_dir():
        return False
    for path, info in native_files(toolkit):
        if info['dwarf']:
            continue
        saved = cache / key(info)
        if not saved.is_file():
            return False
        original = elf(saved)
        if not original['dwarf'] or any(original[k] != info[k] for k in ('build_id', 'identity_type', 'code_sha256')):
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('capture', 'archive', 'provider-ready'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.mode == 'provider-ready':
        raise SystemExit(0 if provider_ready(output) else 1)
    if args.mode == 'capture':
        capture(output)
    else:
        archive(output, output / 'images/reefy-userspace-debug.tar.gz')


if __name__ == '__main__':
    main()
