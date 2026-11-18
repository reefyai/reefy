"""Ensure warm Buildroot output enables the newly required partition utility."""
from pathlib import Path
import subprocess
import sys


def refresh(output):
    output = Path(output).resolve()
    if 'BR2_PACKAGE_UTIL_LINUX_PARTX=y' not in (output / '.config').read_text().splitlines():
        raise RuntimeError('Resolved configuration must include partition utilities')
    installed = any((output / path).is_file() for path in
                    ('target/usr/bin/partx', 'target/usr/sbin/partx', 'target/sbin/partx'))
    if not installed and list((output / 'build').glob('util-linux-*/.stamp_target_installed')):
        subprocess.run(['make', '-C', 'buildroot', 'O=' + str(output),
                        'util-linux-dirclean'], check=True)


if __name__ == '__main__': refresh(sys.argv[1])
