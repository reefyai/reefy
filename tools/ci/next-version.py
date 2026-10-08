#!/usr/bin/env python3
"""Share the existing image sequence across serialized CI output trees."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import tempfile

PATTERN = re.compile(r'^(\d{4}\.\d{2}\.\d{2})-(\d+)$')


def allocate(date, paths):
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
    # Self-hosted firmware jobs are serialized by their single build runner.
    # Mirror the floor to the legacy tree so old-branch builds also advance.
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
