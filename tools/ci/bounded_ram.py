"""Private, size-capped, unswappable Linux scratch for regenerable data only.

Copies in firmware CI and the standalone E2E runner intentionally have no
package dependency. Final outputs must use explicit persistent paths.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

GIB = 1024**3


def cgroup_headroom(directory, root):
    headroom = float('inf')
    while True:
        current = directory / 'memory.current'
        if current.exists():
            used = int(current.read_text())
            for name in ('memory.max', 'memory.high'):
                path = directory / name
                if path.exists() and path.read_text().strip() != 'max':
                    headroom = min(headroom, max(0, int(path.read_text()) - used))
        if directory == root:
            return headroom
        directory = directory.parent


def memory_headroom():
    values = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    available = int(values['MemAvailable'].split()[0]) * 1024
    root = Path('/sys/fs/cgroup')
    group = next(line.split(':', 2)[2] for line in
                 Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
    directory = root / group.lstrip('/')
    # Cgroup namespaces can expose the container root without its host path.
    if not directory.exists():
        directory = root
    return available, cgroup_headroom(directory, root)


def require_capacity(limit, host_reserve=32*GIB, group_reserve=2*GIB):
    host, group = memory_headroom()
    if host < limit + host_reserve or group < limit + group_reserve:
        raise RuntimeError('RAM staging unavailable: insufficient memory headroom; '
                           'retry when healthy, no disk fallback')


def run_in_ram(command, limit):
    """Fail closed on admission/mount/ENOSPC; drain before removing mountpoint."""
    if limit <= 0:
        raise ValueError('RAM staging limit must be positive')
    require_capacity(limit)
    print(f'RAM staging admitted: cap={limit} bytes, private tmpfs, no swap or disk fallback', flush=True)
    # The placeholder is on RAM too. The private mount never reaches the host.
    with tempfile.TemporaryDirectory(prefix='reefy-debug-', dir='/dev/shm') as folder:
        args = ['unshare', '--mount', '--propagation', 'private', '--fork',
                '--kill-child=SIGTERM', sys.executable, str(Path(__file__).resolve()),
                folder, str(limit), json.dumps(list(command))]
        previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
        child = None
        try:
            child = subprocess.Popen(args, start_new_session=True)
            code = child.wait()
            if code:
                raise subprocess.CalledProcessError(code, command)
        finally:
            signal.signal(signal.SIGTERM, previous)
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()


def worker(folder, limit, command):
    # Unshare already made propagation private. noswap prevents a scratch
    # workaround from redirecting this I/O into the backing swap filesystem.
    subprocess.run(['mount', '-t', 'tmpfs', '-o',
                    f'size={limit},nr_inodes=1048576,mode=0700,nosuid,nodev,noexec,noswap',
                    'reefy-debug-scratch', folder], check=True)
    env = dict(os.environ, TMPDIR=folder, TMP=folder, TEMP=folder)
    os.execvpe(command[0], command, env)


if __name__ == '__main__':
    worker(sys.argv[1], int(sys.argv[2]), json.loads(sys.argv[3]))
