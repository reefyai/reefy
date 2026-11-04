#!/usr/bin/env python3
"""Package kernel-reviewed Intel firmware, retaining every selected chip family."""
import argparse
import hashlib
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def policy(path):
    result = json.loads(path.read_text())
    if not result['kernel_ref'].startswith('v') or not result['kernel_sources']:
        raise ValueError('Invalid Intel firmware kernel policy')
    for name in result['files']:
        p = PurePosixPath(name)
        if (p.parent != PurePosixPath('intel/iwlwifi') or
                not re.fullmatch(r'iwlwifi-[\w-]+(?:\.ucode|\.pnvm)', p.name)):
            raise ValueError(f'Invalid managed firmware path: {name}')
    return result


def family(name):
    match = re.fullmatch(r'(iwlwifi-[\w-]+)-(?:c?\d+)\.ucode', Path(name).name)
    return match[1] if match else None


def prepare(archive, rules, kernel_ref, firmware_version):
    if kernel_ref != rules['kernel_ref'] or firmware_version != rules['firmware_version']:
        raise RuntimeError('Kernel/firmware pin changed; review Intel firmware policy before building')
    expected = set(rules['files'])
    expected_families = {family(name) for name in expected if family(name)}
    with tarfile.open(archive) as source:
        members = source.getmembers()
        intel = {item.name: item for item in members
                 if item.name.startswith('intel/iwlwifi/') and item.isfile()}
        actual_families = {family(name) for name in intel if family(name)}
        if actual_families != expected_families:
            raise RuntimeError('Intel chip families changed; review policy instead of dropping coverage')
        actual_pnvm = {name for name in intel if name.endswith('.pnvm')}
        if actual_pnvm != {name for name in expected if name.endswith('.pnvm')}:
            raise RuntimeError('Intel calibration files changed; review firmware policy')
        for name, checksum in rules['files'].items():
            item = intel.get(name)
            if item is None or digest(source.extractfile(item).read()) != checksum:
                raise RuntimeError(f'Missing or changed kernel-compatible firmware: {name}')
        descriptor, temporary = tempfile.mkstemp(prefix='.intel-firmware-', dir=archive.parent)
        os.close(descriptor)
        try:
            with tarfile.open(temporary, 'w') as destination:
                for item in members:
                    if item.name in intel and item.name not in expected:
                        continue
                    destination.addfile(item, source.extractfile(item) if item.isfile() else None)
            os.replace(temporary, archive)
        finally:
            Path(temporary).unlink(missing_ok=True)


def firmware_root(destination, target):
    destination = destination.resolve()
    root = destination / 'lib/firmware' if target else destination
    if not root.resolve().is_relative_to(destination):
        raise RuntimeError('Firmware directory escapes the package destination')
    if not (root / 'intel/iwlwifi').resolve().is_relative_to(root.resolve()):
        raise RuntimeError('Intel firmware directory escapes the package destination')
    return root


def clean(root, rules):
    """Remove only our package's managed names from a warm target/image tree."""
    families = {family(name) for name in rules['files'] if family(name)}
    calibration = {Path(name).name for name in rules['files'] if name.endswith('.pnvm')}
    for directory in (root, root / 'intel/iwlwifi'):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            name = path.name.removesuffix('.xz')
            if family(name) in families or name in calibration:
                if not path.is_file() and not path.is_symlink():
                    raise RuntimeError(f'Managed firmware name is not a file: {path.name}')
                path.unlink()


def install(root, rules):
    # Validate the complete selection before changing any installed file.
    payloads = {}
    for name, checksum in rules['files'].items():
        path = root / name
        if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != checksum:
            raise RuntimeError(f'Missing or changed installed Intel firmware: {name}')
        payloads[name] = lzma.compress(path.read_bytes(), format=lzma.FORMAT_XZ,
                                      check=lzma.CHECK_CRC32, preset=6)
    for name, value in payloads.items():
        path = root / (name + '.xz')
        temporary = path.with_suffix('.xz.tmp')
        temporary.write_bytes(value)
        temporary.replace(path)
    for name in payloads:
        alias = root / (Path(name).name + '.xz')
        alias.unlink(missing_ok=True)
        alias.symlink_to(name + '.xz')
    for name in payloads:
        (root / name).unlink()
        (root / Path(name).name).unlink(missing_ok=True)
    return verify(root, rules)


def verify(root, rules):
    stored = 0
    for name, checksum in rules['files'].items():
        path = root / (name + '.xz')
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f'Compressed Intel firmware missing: {name}')
        value = path.read_bytes()
        if digest(lzma.decompress(value)) != checksum:
            raise RuntimeError(f'Compressed Intel firmware changed: {name}')
        alias = root / (Path(name).name + '.xz')
        if not alias.is_symlink() or alias.resolve() != path.resolve():
            raise RuntimeError(f'Compressed firmware alias missing or incorrect: {name}')
        stored += len(value)
    families = {family(name) for name in rules['files'] if family(name)}
    calibration = {Path(name).name for name in rules['files'] if name.endswith('.pnvm')}
    expected = {Path(name).name + '.xz' for name in rules['files']}
    for directory in (root, root / 'intel/iwlwifi'):
        for path in directory.iterdir():
            name = path.name.removesuffix('.xz')
            if ((family(name) in families or name in calibration)
                    and path.name not in expected):
                raise RuntimeError(f'Obsolete/uncompressed Intel firmware remains: {path.name}')
    return {'files': len(rules['files']), 'families': len(families),
            'compressed_file_bytes': stored, 'kernel_ref': rules['kernel_ref'],
            'firmware_version': rules['firmware_version']}


def check_kernel(kernel, rules):
    for name, checksum in rules['kernel_sources'].items():
        path = kernel / name
        if not path.is_file() or digest(path.read_bytes()) != checksum:
            raise RuntimeError(f'Intel driver source changed; review firmware policy: {name}')
    config = (kernel / '.config').read_text().splitlines()
    for name in ('CONFIG_FW_LOADER', 'CONFIG_FW_LOADER_COMPRESS', 'CONFIG_FW_LOADER_COMPRESS_XZ'):
        if name + '=y' not in config:
            raise RuntimeError(f'Kernel must enable {name}=y for compressed firmware')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'clean', 'install', 'verify', 'check-kernel'))
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--kernel-ref')
    parser.add_argument('--firmware-version')
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--target', action='store_true')
    parser.add_argument('--kernel-dir', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    rules = policy(args.manifest)
    result = None
    if args.operation == 'prepare':
        prepare(args.archive, rules, args.kernel_ref, args.firmware_version)
    elif args.operation == 'check-kernel':
        check_kernel(args.kernel_dir, rules)
    else:
        root = firmware_root(args.destination, args.target)
        if args.operation == 'clean':
            clean(root, rules)
        elif args.operation == 'install':
            result = install(root, rules)
        else:
            result = verify(root, rules)
    if args.report and result:
        args.report.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
