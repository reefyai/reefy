#!/usr/bin/env python3
"""Build a reproducible SquashFS + SHA256 verity tree + RS(255,253) image."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

BLOCK = 4096
ROOTS = 2


def layout(size):
    if size < BLOCK or size > 4 * 1024**3:
        raise ValueError('rootfs must be between 4 KiB and 4 GiB')
    data = (size + BLOCK - 1) // BLOCK
    hashes, level = 0, data
    while level > 1:
        level = (level + 127) // 128
        hashes += level
    fec_start = data + hashes
    parity = ((fec_start + (255 - ROOTS) - 1) // (255 - ROOTS)) * ROOTS
    return data, hashes, fec_start, fec_start + parity


def build(source, output, config, veritysetup):
    started = time.monotonic()
    source, output, config = map(Path, (source, output, config))
    if source.resolve() in (output.resolve(), config.resolve()):
        raise ValueError('output must not overwrite source')
    with source.open('rb') as stream:
        if stream.read(4) != b'hsqs':
            raise ValueError('source is not a SquashFS image')
        stream.seek(0)
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    data, hashes, fec_start, total = layout(source.stat().st_size)
    output.parent.mkdir(parents=True, exist_ok=True)
    config.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        payload = Path(tmp) / 'rootfs.squashfs.verity'
        root_file = Path(tmp) / 'root-hash'
        shutil.copyfile(source, payload)
        # Reserve exactly the data/hash region. veritysetup appends parity.
        with payload.open('r+b') as stream:
            stream.truncate(fec_start * BLOCK)
        options = ['--no-superblock', '--format=1', '--hash=sha256',
                   f'--data-block-size={BLOCK}', f'--hash-block-size={BLOCK}',
                   f'--data-blocks={data}', f'--hash-offset={data * BLOCK}',
                   f'--salt={digest}']
        result = subprocess.run(
            [str(veritysetup), 'format', str(payload), str(payload), *options,
             f'--fec-device={payload}', f'--fec-offset={fec_start * BLOCK}',
             f'--fec-roots={ROOTS}', f'--root-hash-file={root_file}'],
            check=True, capture_output=True, text=True,
            env={**os.environ, 'LC_ALL': 'C'})
        root = root_file.read_text().strip()
        if not re.fullmatch('[0-9a-f]{64}', root):
            raise RuntimeError('veritysetup returned an invalid root digest')
        if payload.stat().st_size != total * BLOCK:
            raise RuntimeError('veritysetup layout differs from expected geometry: '
                               + result.stdout)
        # Verify the finished tree independently, with no FEC to hide build errors.
        subprocess.run([str(veritysetup), 'verify', str(payload), str(payload),
                        root, *options], check=True)
        staged_config = Path(tmp) / 'veritytab'
        # Standard systemd veritytab options are consumed by the native setup
        # helper in our small initramfs; there is no Reefy metadata schema.
        options = [('superblock', 'no'), ('format', 1), ('hash', 'sha256'),
                   ('data-block-size', BLOCK), ('hash-block-size', BLOCK),
                   ('data-blocks', data), ('hash-offset', data * BLOCK),
                   ('salt', digest), ('fec-device', '/rootfs.squashfs.verity'),
                   ('fec-offset', fec_start * BLOCK), ('fec-roots', ROOTS)]
        staged_config.write_text('reefy-root /rootfs.squashfs.verity '
                                 '/rootfs.squashfs.verity ' + root + ' ' +
                                 ','.join(f'{key}={value}' for key, value in options) + '\n')
        os.replace(payload, output)
        # config may be on a different filesystem from output.
        shutil.copyfile(staged_config, config)
    return dict(schema=1, source_sha256=digest, root_hash=root,
                squashfs_bytes=source.stat().st_size, payload_bytes=output.stat().st_size,
                hash_bytes=hashes * BLOCK, fec_bytes=(total - fec_start) * BLOCK,
                generation_and_verification_seconds=time.monotonic() - started)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('config', type=Path)
    parser.add_argument('--veritysetup', required=True)
    parser.add_argument('--metrics', type=Path, required=True)
    args = parser.parse_args()
    metrics = build(args.source, args.output, args.config, args.veritysetup)
    args.metrics.write_text(json.dumps(metrics, indent=2) + '\n')
