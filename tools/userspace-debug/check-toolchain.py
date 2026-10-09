#!/usr/bin/env python3
"""Verify the installed cross compiler emits identities without package flags."""
import argparse
from pathlib import Path
import subprocess
import tempfile

from archive import elf


def check(output):
    compiler = output / 'host/bin/x86_64-buildroot-linux-gnu-gcc'
    with tempfile.TemporaryDirectory(prefix='reefy-toolchain-id-') as directory:
        root = Path(directory)
        source = root / 'synthetic.c'
        source.write_text('int synthetic(void) { return 42; }\n'
                          'int main(void) { return synthetic(); }\n')
        for name, flags in [('synthetic', []), ('libsynthetic.so', ['-shared', '-fPIC'])]:
            binary = root / name
            # Deliberately omit explicit linker/build-ID options. Do not execute
            # target binaries: this verifies actual compiler output only.
            subprocess.run([str(compiler), '-g2', *flags, str(source), '-o', str(binary)],
                           check=True, capture_output=True, timeout=60)
            info = elf(binary)
            if not info or info['identity_type'] != 'gnu' or not info['dwarf']:
                raise RuntimeError('cross compiler must emit GNU identity and DWARF by default')
    print('Toolchain default GNU build IDs and DWARF verified for executable/shared library')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    check(parser.parse_args().output)
