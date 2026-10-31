#!/usr/bin/env python3
"""Allocate unique image sequences across private trees and shared floors."""
import argparse
from contextlib import ExitStack
import fcntl
from datetime import datetime
import os
from pathlib import Path
import re
import tempfile

PATTERN = re.compile(r'^(\d{4}\.\d{2}\.\d{2})-(\d+)$')


def allocate(date, paths):
    # Lock stable sidecar inodes: counters themselves are atomically replaced.
    # Sorted acquisition avoids deadlocks when callers share several floors.
    paths = list(dict.fromkeys(paths))
    with ExitStack() as stack:
        for path in sorted(paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context(Path(str(path) + ".lock").open("a+"))
            fcntl.flock(lock, fcntl.LOCK_EX)
        return _allocate_locked(date, paths)


def _allocate_locked(date, paths):
    datetime.strptime(date, '%Y.%m.%d')
    previous = []
    for path in paths:
        if path.exists():
            match = PATTERN.fullmatch(path.read_text().strip())
            if not match:
                raise ValueError('invalid firmware sequence')
            if match[1] > date:
                raise ValueError('firmware date would go backwards')
            if match[1] == date:
                previous.append(int(match[2]))
    version = f'{date}-{max(previous, default=-1) + 1:02d}'
    # Mirror the floor while all private/shared counters remain locked.
    # A second builder sees the committed shared floor before allocating.
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as file:
                file.write(version + '\n')
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return version


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True)
    parser.add_argument('paths', nargs='+', type=Path)
    args = parser.parse_args()
    try:
        print(allocate(args.date, args.paths))
    except (OSError, ValueError):
        raise SystemExit('Cannot safely allocate firmware version; build stopped') from None
