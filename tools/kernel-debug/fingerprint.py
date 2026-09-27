#!/usr/bin/env python3
"""Conservative kernel cache key over tracked build inputs, not timestamps."""
import hashlib
import subprocess
from pathlib import Path


def fingerprint():
    h = hashlib.sha256(b'reefy-kernel-debug-cache-v1\0')
    # Whole directories intentionally over-invalidate rather than miss patches,
    # provider recipes, kernel fragments, or toolchain configuration changes.
    paths = subprocess.check_output([
        'git', 'ls-files', '-z', 'configs', 'package', 'board',
        'tools/kernel-debug', '.github/workflows/firmware-build.yml',
    ]).split(b'\0')
    for raw in sorted(filter(None, paths)):
        path = Path(raw.decode())
        h.update(raw + b'\0')
        h.update(path.read_bytes() if not path.is_symlink() else str(path.readlink()).encode())
        h.update(b'\0')
    h.update(subprocess.check_output(['git', 'ls-tree', 'HEAD', 'buildroot']))
    return h.hexdigest()


if __name__ == '__main__':
    print(fingerprint())
