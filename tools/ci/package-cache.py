#!/usr/bin/env python3
"""Avoid unconditional refreshes of two expensive, unchanged packages."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

PACKAGES = ('linux-firmware', 'thin-provisioning-tools')


def configuration(output):
    """Hash resolved Kconfig assignments, not generated comments/order."""
    result = {}
    for line in (output / '.config').read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, value = line.partition('=')
        if not separator or not name.replace('_', '').isalnum():
            raise ValueError('invalid resolved Buildroot configuration')
        result[name] = value
    return result


def fingerprint(repo, output):
    digest = hashlib.sha256(b'reefy-package-refresh-v1\0')
    digest.update(json.dumps(configuration(output), sort_keys=True).encode())
    digest.update(subprocess.check_output(['git', 'ls-tree', 'HEAD', 'buildroot'], cwd=repo))
    # Cover external dependencies, patches and hooks conservatively. Runtime
    # overlay edits and tests do not compile either selected package.
    files = subprocess.check_output(['git', 'ls-files', '-z', 'package', 'board',
                                    'external.mk', 'Config.in', 'tools/ci/package-cache.py'], cwd=repo)
    for name in sorted(filter(None, files.split(b'\0'))):
        relative = Path(name.decode())
        if relative.parts[:3] == ('board', 'reefy', 'reefy') and len(relative.parts) > 3:
            if relative.parts[3] in ('rootfs-overlay', 'tests'):
                continue
        path = repo / relative
        digest.update(name + b'\0')
        digest.update(str(path.readlink()).encode() if path.is_symlink() else path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def completed_output(output, package):
    trees = list((output / 'build').glob(package + '-[0-9]*'))
    if len(trees) != 1:
        return None
    tree = trees[0]
    if not (tree / '.stamp_target_installed').is_file():
        return None
    if package == 'linux-firmware':
        if not (tree / '.stamp_images_installed').is_file():
            return None
        path = tree / 'br-firmware.tar'
    else:
        path = output / 'target/usr/bin/pdata_tools'
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def refresh(repo, output, commit=False):
    marker = output / '.reefy-package-refresh.json'
    key = fingerprint(repo, output)
    pending = marker.with_suffix('.pending.json')
    symbols = {name: hashlib.sha256(value.encode()).hexdigest()
               for name, value in configuration(output).items()}
    if commit:
        if pending.exists():
            before = json.loads(pending.read_text())
            changed = sorted(name for name in set(symbols) | set(before.get('symbols', {}))
                             if symbols.get(name) != before.get('symbols', {}).get(name))
            if changed:
                print('package-cache: configuration changed during build: '
                      + ', '.join(changed), flush=True)
        records = {}
        for package in PACKAGES:
            digest = completed_output(output, package)
            if digest is None:
                raise RuntimeError(f'{package}: successful installed output is missing')
            records[package] = digest
        temporary = marker.with_suffix('.tmp')
        temporary.write_text(json.dumps({'inputs': key, 'outputs': records}, sort_keys=True) + '\n')
        temporary.replace(marker)
        pending.unlink(missing_ok=True)
        return
    try:
        previous = json.loads(marker.read_text())
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict) or not isinstance(previous.get('outputs', {}), dict):
        previous = {}
    # A failed build cannot leave a valid record for its next attempt.
    marker.unlink(missing_ok=True)
    pending.write_text(json.dumps({'inputs': key, 'symbols': symbols}, sort_keys=True) + '\n')
    for package in PACKAGES:
        digest = completed_output(output, package)
        if (previous.get('inputs') == key and digest is not None
                and previous.get('outputs', {}).get(package) == digest):
            print(f'{package}: verified unchanged package cache', flush=True)
        else:
            reason = ('input key changed' if previous.get('inputs') != key
                      else 'installed output missing' if digest is None
                      else 'installed output digest changed')
            print(f'{package}: inputs or installed output changed; refreshing ({reason})', flush=True)
            subprocess.run(['make', f'O={output}', package + '-dirclean'], cwd=repo / 'buildroot', check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--commit', action='store_true')
    args = parser.parse_args()
    refresh(Path(__file__).resolve().parents[2], args.output.resolve(), args.commit)
