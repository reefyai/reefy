"""Attach bounded persistent journald storage after Reefy's data mount is ready."""

import json
import os
import subprocess
from pathlib import Path

DATA = Path('/mnt/reefy-data')
JOURNAL = Path('/var/log/journal')
BUDGET_CONFIG = Path('/run/systemd/journald.conf.d/zz-reefy-budget.conf')
MAX_BYTES = 16 * 1024 ** 3
KEEP_FREE_BYTES = 256 * 1024 ** 2
DURABLE_FILESYSTEMS = {'ext4', 'xfs', 'f2fs', 'btrfs'}


def run(command):
    return subprocess.run(command, check=True, capture_output=True,
                          text=True, timeout=20)


def configure_budget():
    filesystem = os.statvfs(DATA)
    available = filesystem.f_bavail * filesystem.f_frsize
    if available <= KEEP_FREE_BYTES:
        return False
    limit = min(MAX_BYTES, available // 10)
    file_limit = min(16 * 1024 ** 2, limit // 8)
    content = (f'[Journal]\nSystemMaxUse={limit}\n'
               f'SystemMaxFileSize={file_limit}\nMaxRetentionSec=90day\n')
    if not BUDGET_CONFIG.exists() or BUDGET_CONFIG.read_text() != content:
        BUDGET_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        BUDGET_CONFIG.write_text(content)
        # Journald has no config reload; restart retains its runtime journal.
        # Do this before binding/flushing, while disk logging is still gated.
        run(['systemctl', 'restart', 'systemd-journald.service'])
    return True


def start():
    # Bootstrap/recovery can leave this directory on RAM-backed rootfs.
    # Never mistake that for the mounted persistent data filesystem.
    mounted = json.loads(run([
        'findmnt', '-J', '-M', str(DATA), '-o', 'TARGET,FSTYPE,OPTIONS',
    ]).stdout).get('filesystems', [])
    if (not mounted or mounted[0]['fstype'] not in DURABLE_FILESYSTEMS
            or 'rw' not in mounted[0]['options'].split(',')):
        print('[journal] Persistent data unavailable; keeping runtime journal')
        return
    if os.path.ismount(JOURNAL):
        # Only reuse our own binding; an unrelated mount is not ours to alter.
        if not os.path.samefile(DATA / 'journal', JOURNAL):
            raise RuntimeError('journal destination is already mounted elsewhere')
    else:
        if not configure_budget():
            print('[journal] Insufficient free space; keeping runtime journal')
            return
        directory = DATA / 'journal'
        directory.mkdir(mode=0o750, exist_ok=True)
        os.chmod(directory, 0o750)
        JOURNAL.mkdir(parents=True, exist_ok=True)
        run(['mount', '--bind', str(directory), str(JOURNAL)])
    try:
        # The normal early flush is gated until this binding exists. Early
        # kernel/userspace entries are still in /run/log/journal at this point.
        run(['journalctl', '--flush'])
    except Exception:
        stop()
        raise
    print('[journal] Runtime journal flushed to persistent data')


def stop():
    if not os.path.ismount(JOURNAL):
        return
    if not os.path.samefile(DATA / 'journal', JOURNAL):
        raise RuntimeError('refusing to detach an unrelated journal mount')
    # Close persistent files before the data filesystem is unmounted.
    run(['journalctl', '--relinquish-var'])
    run(['umount', str(JOURNAL)])


def main():
    import sys
    try:
        stop() if sys.argv[1:] == ['stop'] else start()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        # Logging must not prevent storage, control or workloads from starting.
        # Do not print command output: it may contain device-specific data.
        print('[journal] Persistent journal operation failed; boot continues')


if __name__ == '__main__':
    main()
