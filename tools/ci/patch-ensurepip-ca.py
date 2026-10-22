#!/usr/bin/env python3
"""Backport upstream certifi CA data inside Python's unchanged bootstrap wheel."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import zipfile

BLOCKED = '86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b'
BUNDLE = 'pip/_vendor/certifi/cacert.pem'


def certificates(data):
    return {hashlib.sha256(ssl.PEM_cert_to_DER_cert(p.decode())).hexdigest(): p
            for p in re.findall(b'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', data, re.S)}


def repack(wheel, patches, blocked=BLOCKED):
    before_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with zipfile.ZipFile(wheel) as archive:
        before = {name: archive.read(name) for name in archive.namelist() if not name.endswith('/')}
    roots = certificates(before[BUNDLE])
    if blocked not in roots:
        return {'changed': False, 'sha256': before_sha, 'roots': len(roots)}
    if any(name.endswith(('RECORD.jws', 'RECORD.p7s')) for name in before):
        raise RuntimeError('cannot modify a signed bootstrap wheel')
    records = [name for name in before if name.endswith('.dist-info/RECORD')]
    if len(records) != 1:
        raise RuntimeError('bootstrap wheel has no unique RECORD')
    with tempfile.TemporaryDirectory(prefix='reefy-ensurepip-ca-') as folder:
        folder = Path(folder)
        unpacked = folder / 'unpacked'
        # The standard wheel tools verify existing RECORD hashes and regenerate
        # them after applying the unchanged upstream CA-data hunks.
        subprocess.run([sys.executable, '-m', 'wheel', 'unpack', str(wheel), '-d', str(unpacked)], check=True)
        trees = list(unpacked.iterdir())
        if len(trees) != 1:
            raise RuntimeError('unexpected unpacked bootstrap wheel layout')
        tree = trees[0]
        for patch in patches:
            # Source patches address src/pip; a wheel starts directly at pip.
            subprocess.run(['patch', '--batch', '--fuzz=0', '--no-backup-if-mismatch', '-p2', '-i', str(patch.resolve())], cwd=tree, check=True)
        fixed = certificates((tree / BUNDLE).read_bytes())
        if set(roots) - set(fixed) != {blocked} or set(fixed) - set(roots):
            raise RuntimeError('unexpected bootstrap CA additions/removals')
        if any(roots[key] != value for key, value in fixed.items()):
            raise RuntimeError('unrelated bootstrap certificate bytes changed')
        packed = folder / 'packed'
        packed.mkdir()
        subprocess.run([sys.executable, '-m', 'wheel', 'pack', str(tree), '-d', str(packed)], check=True)
        replacement = packed / wheel.name
        with zipfile.ZipFile(replacement) as archive:
            after = {name: archive.read(name) for name in archive.namelist() if not name.endswith('/')}
        if before.keys() != after.keys() or any(before[name] != after[name]
                for name in before if name not in (BUNDLE, records[0])):
            raise RuntimeError(f'unrelated bootstrap wheel payload changed: added={set(after)-set(before)}, removed={set(before)-set(after)}, modified={[name for name in before.keys() & after.keys() if before[name] != after[name] and name not in (BUNDLE, records[0])]}')
        subprocess.run([sys.executable, '-m', 'wheel', 'unpack', str(replacement), '-d', str(folder / 'verified')], check=True)
        pending = wheel.with_suffix('.whl.pending')
        shutil.copyfile(replacement, pending)
        shutil.copystat(wheel, pending)
        pending.replace(wheel)
    return {'changed': True, 'before_sha256': before_sha,
            'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
            'roots_before': len(roots), 'roots': len(fixed),
            'unrelated_roots_and_payload_unchanged': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--patch-dir', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    patches = sorted(args.patch_dir.glob('*.patch'))
    if len(patches) != 2:
        raise RuntimeError('expected the two upstream certifi data backports')
    results = {}
    for wheel in sorted(args.root.glob('usr/lib/python*/ensurepip/_bundled/pip-*.whl')):
        results[str(wheel.relative_to(args.root))] = repack(wheel, patches)
    bundles = {}
    for bundle in sorted(args.root.glob('usr/lib/python*/**/cacert.pem')):
        data = bundle.read_bytes()
        roots = certificates(data)
        if BLOCKED in roots:
            raise RuntimeError(f'unfixed installed Python CA bundle: {bundle.relative_to(args.root)}')
        bundles[str(bundle.relative_to(args.root))] = {'sha256': hashlib.sha256(data).hexdigest(), 'roots': len(roots)}
    report = {'bundles': bundles, 'wheels': results, 'patch_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in patches}}
    args.report.write_text(json.dumps(report, indent=2) + '\n')
