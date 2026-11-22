#!/usr/bin/env python3
"""Conservative kernel cache key over tracked build inputs, not timestamps."""
import argparse
import hashlib
import json
import sys
import subprocess
from pathlib import Path


def kernel_input(path):
    # Exclude only known non-build trees. Unknown board inputs remain covered,
    # including new patches/config fragments and pre/post-build hooks.
    parts = path.parts
    # These tools only package/benchmark already compiled outputs. They cannot
    # change kernel Kconfig, compilation or module ABI. Unknown tools stay in.
    if parts[:2] in (('tools', 'ci'), ('tools', 'kernel-debug')):
        if path.name.startswith('test_') or path.name.startswith('README'):
            return False
    if path.as_posix() in {
        'tools/kernel-debug/archive.py', 'tools/ci/archive-pair.py',
        'tools/ci/archive-debug.sh', 'tools/ci/bounded_ram.py',
        'tools/ci/benchmark-debug-archives.py',
    }:
        return False
    if parts[:3] == ('board', 'reefy', 'reefy') and len(parts) > 3:
        return parts[3] not in ('rootfs-overlay', 'tests')
    return True


def fingerprint(output=None, components=None):
    h = hashlib.sha256(b'reefy-kernel-debug-cache-v2\0')
    # Retain conservative coverage of build recipes, patches and configuration.
    # Runtime overlay code and tests do not compile the kernel or modules.
    paths = subprocess.check_output([
        'git', 'ls-files', '-z', 'configs', 'package', 'board',
        'tools/kernel-debug', 'tools/ci', '.github/workflows/firmware-build.yml',
        'external.mk', 'external.desc', 'Config.in',
    ]).split(b'\0')
    for raw in sorted(filter(None, paths)):
        path = Path(raw.decode())
        if not kernel_input(path):
            continue
        h.update(raw + b'\0')
        content = path.read_bytes() if not path.is_symlink() else str(path.readlink()).encode()
        h.update(content)
        if components is not None:
            components['file:' + path.as_posix()] = hashlib.sha256(content).hexdigest()
        h.update(b'\0')
    tree = subprocess.check_output(['git', 'ls-tree', 'HEAD', 'buildroot'])
    h.update(tree)
    if components is not None:
        components['buildroot'] = hashlib.sha256(tree).hexdigest()
    if output is not None:
        # Match the Rust compiler on Buildroot's kernel PATH. Its presence and
        # capabilities affect resolved Kconfig even for a C-only kernel.
        compiler = Path(output).resolve() / 'host/bin/rustc'
        identity = subprocess.check_output([str(compiler), '--version', '--verbose'])
        h.update(b'kernel-rust-toolchain\0' + identity + b'\0')
        compiler_digest = hashlib.sha256(compiler.read_bytes()).digest()
        h.update(compiler_digest)
        if components is not None:
            components['rust:identity'] = hashlib.sha256(identity).hexdigest()
            components['rust:binary'] = compiler_digest.hex()
    return h.hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    output = parser.parse_args().output
    components = {}
    key = fingerprint(output, components)
    if output is not None:
        previous = output / '.reefy-kernel-input-components.json'
        try:
            before = json.loads(previous.read_text())
            if not isinstance(before, dict):
                before = {}
        except (OSError, ValueError):
            before = {}
        changed = sorted(name for name in before.keys() | components.keys()
                         if before.get(name) != components.get(name))
        if before and changed:
            print('kernel-cache: changed input names: ' + ', '.join(changed), file=sys.stderr)
        elif not before:
            print('kernel-cache: previous input components unavailable', file=sys.stderr)
        (output / '.reefy-kernel-input-components.pending.json').write_text(
            json.dumps(components, sort_keys=True) + '\n')
    print(key)
