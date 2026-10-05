#!/usr/bin/env python3
"""Conservative kernel cache key over tracked build inputs, not timestamps."""
import hashlib
import subprocess
from pathlib import Path


def kernel_input(path):
    # Exclude only known non-build trees. Unknown board inputs remain covered,
    # including new patches/config fragments and pre/post-build hooks.
    parts = path.parts
    if parts[:3] == ('board', 'reefy', 'reefy') and len(parts) > 3:
        return parts[3] not in ('rootfs-overlay', 'tests')
    return True


def fingerprint():
    h = hashlib.sha256(b'reefy-kernel-debug-cache-v2\0')
    # Retain conservative coverage of build recipes, patches and configuration.
    # Runtime overlay code and tests do not compile the kernel or modules.
    paths = subprocess.check_output([
        'git', 'ls-files', '-z', 'configs', 'package', 'board',
        'tools/kernel-debug', '.github/workflows/firmware-build.yml',
        'external.mk', 'external.desc', 'Config.in',
    ]).split(b'\0')
    for raw in sorted(filter(None, paths)):
        path = Path(raw.decode())
        if not kernel_input(path):
            continue
        h.update(raw + b'\0')
        h.update(path.read_bytes() if not path.is_symlink() else str(path.readlink()).encode())
        h.update(b'\0')
    h.update(subprocess.check_output(['git', 'ls-tree', 'HEAD', 'buildroot']))
    return h.hexdigest()


if __name__ == '__main__':
    print(fingerprint())
