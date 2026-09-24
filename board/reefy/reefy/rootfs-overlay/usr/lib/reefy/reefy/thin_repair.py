"""LVM repair hook: never replace current mappings with guessed history.

Native thin_repair may reconstruct an older tree if the superblock or its
roots are unreadable. Only permit automatic repair when ordinary thin_dump
can read the authoritative input and native repair preserves its full dump.
Space-map repair remains possible; lost mapping roots require an operator.
"""
import argparse
import hashlib
import subprocess
import sys


TOOLS = '/usr/sbin/'


def fingerprint(path):
    # No --repair, geometry overrides, skip-mappings, or ignore-errors here.
    # The same pinned native dumper normalizes both trees. Stream its output
    # rather than keeping potentially large mapping dumps on the RAM root.
    digest = hashlib.sha256()
    with subprocess.Popen([TOOLS + 'thin_dump', path], stdout=subprocess.PIPE) as process:
        while chunk := process.stdout.read(64 * 1024):
            digest.update(chunk)
        if process.wait():
            raise RuntimeError('current superblock/mapping trees are unreadable; '
                               'refusing historical thin-metadata reconstruction')
    return digest.hexdigest()


def repair(source, destination):
    # LVM supplies an inactive metadata LV and a separate repair spare. Never
    # override the superblock's transaction or geometry from the VG record.
    subprocess.run([TOOLS + 'thin_check', '--super-block-only', source], check=True)
    before = fingerprint(source)
    print('[reefy] Thin repair input mappings sha256=' + before, flush=True)
    subprocess.run([TOOLS + 'thin_repair', '-i', source, '-o', destination], check=True)
    subprocess.run([TOOLS + 'thin_check', destination], check=True)
    after = fingerprint(destination)
    if before != after:
        raise RuntimeError('native thin repair changed the current mapping dump; '
                           'refusing metadata swap')
    print('[reefy] Verified thin repair preserves current mappings sha256=' + after,
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # LVM requires a nonempty repair-options array. Keep the policy explicit
    # in its invocation instead of supplying unsafe geometry overrides.
    parser.add_argument('--preserve-mappings', action='store_true', required=True)
    parser.add_argument('-i', '--input', required=True)
    parser.add_argument('-o', '--output', required=True)
    args = parser.parse_args()
    try:
        repair(args.input, args.output)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print('[reefy] ERROR: Automatic thin repair refused: ' + str(error),
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
