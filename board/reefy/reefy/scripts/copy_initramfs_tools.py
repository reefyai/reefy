#!/usr/bin/env python3
"""Copy target ELF executables and their dependency closure, without executing them."""
import argparse
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess


def resolve(root, logical):
    parts = list(PurePosixPath(logical).parts[1:])
    current = root
    links = 0
    while parts:
        current = current / parts.pop(0)
        if current.is_symlink():
            links += 1
            if links > 40:
                raise ValueError('symlink loop in target tools')
            dest = os.readlink(current)
            relative = (PurePosixPath(dest) if dest.startswith('/') else
                        PurePosixPath('/') / current.parent.relative_to(root) / dest)
            normal = os.path.normpath(str(relative))
            current = root
            parts = list(PurePosixPath(normal).parts[1:]) + parts
    return current


def copy_tools(root, destination, readelf, tools):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    # Match the target's library aliases. Flattening /lib64 into a separate
    # directory breaks loaders whose native search path uses /lib64 while the
    # dependency resolver found the same library through /lib -> /usr/lib.
    for logical in ('/lib', '/lib64', '/usr/lib', '/usr/lib64'):
        source = root / logical.lstrip('/')
        if not source.is_symlink():
            continue
        target = os.readlink(source)
        absolute = os.path.normpath(str(PurePosixPath(logical).parent / target))
        relative = os.path.relpath(absolute, str(PurePosixPath(logical).parent))
        output = destination / logical.lstrip('/')
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.is_symlink() and output.readlink() == Path(relative):
            continue
        output.symlink_to(relative)
    pending = list(tools)
    copied = set()
    while pending:
        logical = pending.pop()
        if logical in copied:
            continue
        source = resolve(root, logical)
        if not source.is_file():
            raise FileNotFoundError(f'missing initramfs dependency {logical}')
        output = resolve(destination, logical)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
        copied.add(logical)
        result = subprocess.run([readelf, '-l', '-d', str(source)],
                                check=True, capture_output=True, text=True)
        interpreter = re.search(r'Requesting program interpreter: ([^\]]+)', result.stdout)
        if interpreter:
            pending.append(interpreter[1])
        origin = '/' + str(source.parent.relative_to(root))
        search = []
        for value in re.findall(r'\((?:RUNPATH|RPATH)\).*?\[([^\]]+)\]', result.stdout):
            for directory in value.split(':'):
                directory = directory.replace('${ORIGIN}', origin).replace('$ORIGIN', origin)
                if '$' in directory or not directory.startswith('/'):
                    raise ValueError(f'unsupported target library search path: {directory}')
                search.append(os.path.normpath(directory))
        search += ['/lib', '/usr/lib', '/lib64', '/usr/lib64']
        for name in re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', result.stdout):
            candidates = [f'{directory}/{name}' for directory in search]
            selected = next((p for p in candidates if resolve(root, p).is_file()), None)
            if selected is None:
                raise FileNotFoundError(f'unresolved dependency {name} for {logical}')
            pending.append(selected)
    return sorted(copied)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', type=Path)
    parser.add_argument('initramfs', type=Path)
    parser.add_argument('--readelf', required=True)
    parser.add_argument('tools', nargs='+')
    args = parser.parse_args()
    copy_tools(args.target, args.initramfs, args.readelf, args.tools)
