#!/usr/bin/env python3
"""Invalidate cached target systemd when its EFI option changes."""
import argparse
from pathlib import Path
import re
import subprocess


def check(output, verify=False):
    expected = int('BR2_PACKAGE_SYSTEMD_EFI=y' in (output / '.config').read_text().splitlines())
    packages = list((output / 'build').glob('systemd-[0-9]*'))
    if verify and not packages:
        raise RuntimeError('target systemd build configuration is missing')
    for package in packages:
        header = package / 'buildroot-build/config.h'
        match = re.search(r'^#define ENABLE_EFI ([01])$', header.read_text(), re.M) if header.exists() else None
        actual = int(match[1]) if match else None
        if actual == expected:
            continue
        if verify:
            raise RuntimeError(f'compiled systemd ENABLE_EFI={actual}, expected {expected}')
        if (package / '.stamp_configured').exists():
            print(f'invalidating cached systemd ENABLE_EFI={actual}, expected {expected}', flush=True)
            subprocess.run(['make', f'O={output}', 'systemd-dirclean'], check=True)
            break


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    check(args.output.resolve(), args.verify)
