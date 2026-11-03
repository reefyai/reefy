#!/usr/bin/env python3
"""Compare Intel firmware packaging options without changing production files."""
import argparse
import hashlib
import json
import lzma
from pathlib import Path
import re
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--mksquashfs', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    args = parser.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=False)
    previous = json.loads(args.inventory.read_text())
    base = previous['groups']['baseline']['inventory']
    entries = {path: entry for path, entry in previous['groups']['intel']['inventory'].items()
               if path not in base and 'bytes' in entry}
    data = {}
    with tarfile.open(args.archive) as archive:
        for path, entry in entries.items():
            name = Path(path).name
            stream = archive.extractfile(name)
            value = stream.read()
            if hashlib.sha256(value).hexdigest() != entry['sha256']:
                raise RuntimeError(f'Firmware differs from verified original: {name}')
            data[path] = value

    families = {}
    ancillary = []
    for path in entries:
        match = re.fullmatch(r'(.*)-(c?\d+)\.ucode', path)
        if match:
            families.setdefault(match[1], []).append((match[2], path))
        else:
            ancillary.append(path)
    if len(ancillary) != 3 or any(not item.endswith('.pnvm') for item in ancillary):
        raise RuntimeError('Recheck ancillary firmware assumptions')
    selected = list(ancillary)
    fallback = list(ancillary)
    retained = {}
    for prefix, candidates in sorted(families.items()):
        name = Path(prefix).name
        # Limits are from Linux v6.18.60 cfg/ files and iwl-drv.c's MAC/RF
        # range intersection. Wi-Fi 7 can fall back from c99 to API 101.
        # Do not select the numerically newest c106 file for this kernel.
        if name == 'iwlwifi-gl-c0-fm-c0':
            lower, upper = 100, 101
        elif name in ('iwlwifi-so-a0-gf-a0', 'iwlwifi-ty-a0-gf-a0'):
            lower = upper = 89
        elif name.startswith(('iwlwifi-Qu-', 'iwlwifi-QuZ-', 'iwlwifi-cc-', 'iwlwifi-so-a0-jf-')):
            lower = upper = 77
        elif name.startswith(('iwlwifi-9000-', 'iwlwifi-9260-')):
            lower, upper = 30, 46
        elif name in ('iwlwifi-8000C', 'iwlwifi-8265'):
            lower, upper = 22, 36
        else:
            # Each older prefix has exactly one file in the original bundle.
            if len(candidates) != 1 or candidates[0][0].startswith('c'):
                raise RuntimeError(f'Review kernel API limits for {prefix}')
            lower = upper = int(candidates[0][0])
        compatible = sorted((int(api), path) for api, path in candidates
                            if not api.startswith('c') and lower <= int(api) <= upper)
        if not compatible:
            raise RuntimeError(f'No compatible file for {prefix}')
        selected.append(compatible[-1][1])
        fallback.extend(path for _, path in compatible[-2:])
        retained[name] = {'available_files': len(candidates),
                          'kernel_api_range': [lower, upper],
                          'selected_file': Path(compatible[-1][1]).name}

    xz = {path: lzma.compress(value, format=lzma.FORMAT_XZ,
                             check=lzma.CHECK_CRC32, preset=6)
          for path, value in data.items()}
    for path, value in xz.items():
        if lzma.decompress(value) != data[path]:
            raise RuntimeError(f'Compressed firmware changed content: {path}')
    variants = {
        'all_versions': (list(entries), False, '128K'),
        'kernel_selected': (selected, False, '128K'),
        'kernel_selected_with_compatible_fallback': (fallback, False, '128K'),
        'all_versions_per_file_xz': (list(entries), True, '128K'),
        'kernel_selected_per_file_xz': (selected, True, '128K'),
        'all_versions_1m_squashfs_blocks': (list(entries), False, '1M'),
    }
    reports = {}
    for name, (paths, compress, block) in variants.items():
        root = args.scratch / name
        for path in paths:
            suffix = '.xz' if compress else ''
            target = root / (path + suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(xz[path] if compress else data[path])
            # WHENCE root aliases need the same suffix for compressed lookup.
            link = root / 'lib/firmware' / (Path(path).name + suffix)
            link.symlink_to('intel/iwlwifi/' + target.name)
            if not link.is_file():
                raise RuntimeError(f'Broken firmware alias: {link}')
        image = root.with_suffix('.squashfs')
        command = [str(args.mksquashfs), str(root), str(image), '-noappend',
                   '-comp', 'xz', '-b', block, '-processors', '4', '-all-root',
                   '-mkfs-time', '0', '-all-time', '0', '-no-progress']
        with (args.scratch / (name + '.log')).open('w') as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
        reports[name] = {
            'files': len(paths),
            'regular_file_bytes': sum(len(xz[path] if compress else data[path]) for path in paths),
            'squashfs_bytes': image.stat().st_size,
            'retained_families': len({re.sub(r'-(?:c?\d+)\.ucode$', '', path)
                                      for path in paths if path.endswith('.ucode')}),
        }
        print(name, reports[name], flush=True)
    for report in reports.values():
        report['saved_squashfs_bytes'] = reports['all_versions']['squashfs_bytes'] - report['squashfs_bytes']
    result = {
        'kernel': 'v6.18.60', 'firmware': previous['firmware_version'],
        'method': 'Intel-only SquashFS XZ trees with root aliases; all input files match the verified earlier inventory; firmware loading not hardware-tested',
        'input_inventory_sha256': hashlib.sha256(args.inventory.read_bytes()).hexdigest(),
        'subtree_archive_sha256': hashlib.sha256(args.archive.read_bytes()).hexdigest(),
        'research_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'variants': reports, 'families': retained,
    }
    (args.scratch / 'comparison.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
