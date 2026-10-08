"""Attach stock systemd crash storage without losing the bounded early spool."""

import fcntl
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

DATA = Path('/mnt/reefy-data')
RUNTIME = Path('/run/reefy-coredumps')
DESTINATION = Path('/var/lib/systemd/coredump')
CONFIG = Path('/run/systemd/coredump.conf.d/zz-reefy-budget.conf')
LOCK = Path('/run/reefy-coredump-attach.lock')
MAX_BYTES = 4 * 1024 ** 3
KEEP_FREE = 512 * 1024 ** 2
FILESYSTEMS = {'ext4', 'xfs', 'f2fs', 'btrfs'}


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=20)


def directory(path):
    if path.is_symlink():
        raise RuntimeError('refusing symlink crash directory')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def early():
    directory(RUNTIME)
    if not os.path.ismount(RUNTIME):
        run(['mount', '-t', 'tmpfs', '-o', 'size=256M,mode=0700,nodev,nosuid,noexec',
             'reefy-coredumps', str(RUNTIME)])
    # A bind of a shared mount inherits its peer group. Without isolation,
    # stacking disk storage over DESTINATION also propagates over RUNTIME,
    # hiding the early cores before migrate() can read them.
    run(['mount', '--make-private', str(RUNTIME)])
    directory(DESTINATION)
    if not os.path.ismount(DESTINATION):
        run(['mount', '--bind', str(RUNTIME), str(DESTINATION)])
    run(['mount', '--make-private', str(DESTINATION)])


def budget(available):
    if available <= KEEP_FREE:
        return None
    total = min(MAX_BYTES, available // 20, available - KEEP_FREE)
    # Bound the largest uncompressed core too; native MaxUse permits temporary
    # overshoot while an individual dump is being written.
    if total < 64 * 1024 ** 2:
        return None
    return total, min(2 * 1024 ** 3, total // 2)


def migrate(destination):
    # Native systemd publishes completed core.* files atomically. Do not move
    # temporary files or unmount the spool: an early worker can still finish
    # there in its private mount namespace. The timer collects those later.
    for source in RUNTIME.glob('core.*'):
        if not stat.S_ISREG(source.lstat().st_mode):
            continue
        final = destination / source.name
        temporary = destination / ('.reefy-transfer-' + source.name)
        if final.exists():
            # Never overwrite ambiguous prior evidence or remove its source.
            continue
        try:
            shutil.copy2(source, temporary, follow_symlinks=False)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, final)
            descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            source.unlink()
        finally:
            temporary.unlink(missing_ok=True)


def configure(limits):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    content = (f'[Coredump]\nMaxUse={limits[0]}\nExternalSizeMax={limits[1]}\n'
               f'KeepFree={KEEP_FREE}\n')
    temporary = CONFIG.with_suffix('.tmp')
    temporary.write_text(content)
    os.replace(temporary, CONFIG)


def attach():
    mounted = json.loads(run(['findmnt', '-J', '-M', str(DATA),
                              '-o', 'TARGET,FSTYPE,OPTIONS']).stdout).get('filesystems', [])
    if (not mounted or mounted[0]['fstype'] not in FILESYSTEMS
            or 'rw' not in mounted[0]['options'].split(',')):
        return
    filesystem = os.statvfs(DATA)
    limits = budget(filesystem.f_bavail * filesystem.f_frsize)
    if limits is None:
        return
    destination = DATA / 'coredumps'
    directory(destination)
    if os.path.ismount(DESTINATION):
        if os.path.samefile(destination, DESTINATION):
            configure(limits)
            migrate(destination)
            return
        if not os.path.samefile(RUNTIME, DESTINATION):
            raise RuntimeError('refusing unrelated crash-storage mount')
    else:
        early()
    # Mount on top of our own RAM binding. Existing workers retain their spool
    # namespace; new workers see persistent storage. The spool remains bounded.
    run(['mount', '--make-private', str(DESTINATION)])
    run(['mount', '--bind', str(destination), str(DESTINATION)])
    run(['mount', '--make-private', str(DESTINATION)])
    configure(limits)
    # Coredump workers read config at startup; no daemon restart is needed.
    migrate(destination)


def stop():
    destination = DATA / 'coredumps'
    if os.path.ismount(DESTINATION) and destination.exists():
        if os.path.samefile(destination, DESTINATION):
            migrate(destination)
            run(['umount', str(DESTINATION)])
            CONFIG.unlink(missing_ok=True)
        elif not os.path.samefile(RUNTIME, DESTINATION):
            raise RuntimeError('refusing unrelated crash-storage mount')


def main():
    import sys
    try:
        with LOCK.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            {'early': early, 'stop': stop}.get(
                sys.argv[1] if len(sys.argv) > 1 else '', attach)()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        # Crash diagnostics must not block storage or workloads. Never log
        # collector output or filenames, which can contain private process data.
        print('[coredump] Crash storage attachment failed; boot continues')


if __name__ == '__main__':
    main()
