#!/usr/bin/env python3
"""Compare archive creation on one immutable cached build, with bounded CPUs."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time

SOURCE = Path('/buildroot-cache/output-buildroot-2026.08')
RESULTS = Path('archive-benchmark-results')


def inventory(path):
    with tarfile.open(path, 'r:gz') as archive:
        member = next(x for x in archive.getmembers() if x.name.lstrip('./') == 'SHA256SUMS')
        entries = archive.extractfile(member).read().decode().splitlines()
        return sorted(x for x in entries if not x.endswith('  metadata.json'))


def main():
    RESULTS.mkdir(exist_ok=False)
    cpus = sorted(os.sched_getaffinity(0))[:16]
    assert cpus and SOURCE.is_dir()
    identity = (SOURCE/'target/usr/lib/os-release').read_bytes()
    summary = {'source': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
               'input_os_release_sha256': hashlib.sha256(identity).hexdigest(),
               'cpu_budget': len(cpus), 'trials': []}
    expected = None
    for number, mode in enumerate(('sequential', 'parallel', 'parallel', 'sequential')):
        folder = RESULTS/f'{number}-{mode}'
        output = folder/'output'
        (output/'images').mkdir(parents=True)
        for name in ('build', 'host', 'target', 'reefy-artifacts', 'reefy-userspace-symbols', '.config'):
            (output/name).symlink_to(SOURCE/name)
        for original in (SOURCE/'images').iterdir():
            if original.name not in ('reefy-kernel-debug.tar.gz', 'reefy-userspace-debug.tar.gz', 'reefy-userspace-debug.json'):
                (output/'images'/original.name).symlink_to(original)
        def run(kind):
            command = ['taskset', '-c', ','.join(map(str, cpus)), 'python3',
                       f'tools/{kind}-debug/archive.py']
            if kind == 'userspace':
                command.append('archive')
            command.extend(['--output', str(output.resolve())])
            started = time.monotonic()
            with (folder/f'{kind}.log').open('w') as log:
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
            return time.monotonic()-started
        started = time.monotonic()
        if mode == 'parallel':
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = {kind: pool.submit(run, kind) for kind in ('kernel', 'userspace')}
                elapsed = {kind: future.result() for kind, future in futures.items()}
        else:
            elapsed = {kind: run(kind) for kind in ('kernel', 'userspace')}
        wall = time.monotonic()-started
        actual = {kind: inventory(output/'images'/f'reefy-{kind}-debug.tar.gz')
                  for kind in ('kernel', 'userspace')}
        if expected is None:
            expected = actual
        assert actual == expected, 'archive payload inventories changed'
        assert (SOURCE/'target/usr/lib/os-release').read_bytes() == identity
        summary['trials'].append({'mode': mode, 'elapsed_s': wall, 'archives_s': elapsed,
                                  'payload_inventories_match': True})
        (RESULTS/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
        print(json.dumps(summary['trials'][-1]), flush=True)
        for artifact in (output/'images').glob('*debug.tar.gz'):
            artifact.unlink()


if __name__ == '__main__':
    main()
