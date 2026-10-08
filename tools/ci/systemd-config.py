#!/usr/bin/env python3
"""Invalidate cached target systemd when its compiled feature options change."""
import argparse
from pathlib import Path
import re
import subprocess


def check(output, verify=False):
    configuration = (output / '.config').read_text().splitlines()
    expected = {feature: int('BR2_PACKAGE_SYSTEMD_' + feature + '=y' in configuration)
                for feature in ('EFI', 'COREDUMP')}
    packages = list((output / 'build').glob('systemd-[0-9]*'))
    if verify and not packages:
        raise RuntimeError('target systemd build configuration is missing')
    for package in packages:
        header = package / 'buildroot-build/config.h'
        content = header.read_text() if header.exists() else ''
        mismatches = []
        for feature, wanted in expected.items():
            match = re.search(r'^#define ENABLE_' + feature + r' ([01])$', content, re.M)
            actual = int(match[1]) if match else None
            if actual != wanted:
                mismatches.append(f'ENABLE_{feature}={actual}, expected {wanted}')
        if not mismatches:
            continue
        reason = '; '.join(mismatches)
        if verify:
            raise RuntimeError(f'compiled systemd {reason}')
        if (package / '.stamp_configured').exists():
            print(f'invalidating cached systemd {reason}', flush=True)
            subprocess.run(['make', f'O={output}', 'systemd-dirclean'], check=True)
            break



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    check(args.output.resolve(), args.verify)
