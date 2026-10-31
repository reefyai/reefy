#!/usr/bin/env python3
"""Create both verified archives on one immutable output with bounded CPUs."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def run_pair(commands, logs, cpus):
    """Drain every child on success, failure or interruption; retain logs."""
    logs.mkdir(parents=True, exist_ok=True)
    children = {}
    started = time.monotonic()
    with ExitStack() as stack:
        try:
            for name, command in commands.items():
                log = stack.enter_context((logs / (name + '.log')).open('w'))
                children[name] = subprocess.Popen(
                    ['taskset', '-c', ','.join(map(str, cpus)), *command],
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            while children:
                states = {name: child.poll() for name, child in children.items()}
                failed = [name for name, code in states.items()
                          if code is not None and code != 0]
                if failed:
                    raise RuntimeError('Debug archive failed: ' + ', '.join(failed))
                if all(code == 0 for code in states.values()):
                    break
                time.sleep(.05)
        finally:
            for child in children.values():
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            for child in children.values():
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
    return {'elapsed_s': round(time.monotonic() - started, 3),
            'cpu_budget': len(cpus), 'archives_verified': sorted(commands)}


def main():
    output = Path(os.environ['BR_OUTPUT'])
    cpus = sorted(os.sched_getaffinity(0))[:16]
    commands = {
        'kernel': [sys.executable, 'tools/kernel-debug/archive.py',
                   '--output', str(output)],
        'userspace': [sys.executable, 'tools/userspace-debug/archive.py',
                      'archive', '--output', str(output)],
    }
    Path('archive-pair-logs/summary.json').unlink(missing_ok=True)
    result = run_pair(commands, Path('archive-pair-logs'), cpus)
    Path('archive-pair-logs/summary.json').write_text(json.dumps(result) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
