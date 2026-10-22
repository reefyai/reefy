#!/usr/bin/env python3
"""Rebuild Go consumers when the pinned compiler changes in a warm build."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import shutil


def version(repo):
    text = (repo / 'buildroot/package/go/go.mk').read_text()
    return re.search(r'^GO_VERSION = ([0-9.]+)$', text, re.M)[1]


def refresh(repo, output, commit=False):
    expected = version(repo)
    marker = output / '.reefy-go-version'
    if commit:
        marker.unlink(missing_ok=True)
        compiler = output / 'host/lib/go/bin/go'
        actual = subprocess.check_output([str(compiler), 'version'], text=True).split()[2]
        if actual != 'go' + expected:
            raise RuntimeError(f'compiler version mismatch: {actual}')
        result = subprocess.check_output(
            [str(compiler), 'version', '-m', str(output / 'target')], text=True)
        binaries = dict(re.findall(r'^(.+): (go\S+)$', result, re.M))
        if not binaries:
            raise RuntimeError('no target Go binaries found')
        stale = {path: value for path, value in binaries.items() if value != 'go' + expected}
        (output / 'reefy-go-binaries.json').write_text(json.dumps(binaries, indent=2) + '\n')
        if stale:
            raise RuntimeError(f'target binaries use a stale Go compiler: {stale}')
        marker.write_text(expected + '\n')
        return
    if marker.exists() and marker.read_text().strip() == expected and (output / 'host/lib/go/bin/go').is_file():
        print(f'Go {expected}: verified compiler cache marker unchanged', flush=True)
        return
    # Only a successful build may establish the new version marker.
    marker.unlink(missing_ok=True)
    consumers = subprocess.check_output(
        ['make', '-s', f'O={output}', 'host-go-show-recursive-rdepends'],
        cwd=repo / 'buildroot', text=True).split()
    if not all(re.fullmatch(r'[a-z0-9][a-z0-9+_.-]*', name) for name in consumers):
        raise RuntimeError('invalid Buildroot Go consumer list')
    # Upstream installs Go sources with cp -a. Replace the package-owned root
    # before switching versions so deleted standard-library files cannot survive.
    compiler_root = output / 'host/lib/go'
    if compiler_root.is_symlink():
        compiler_root.unlink()
    elif compiler_root.exists():
        shutil.rmtree(compiler_root)
    for name in sorted(set(consumers) | {'host-go-bin', 'host-go-src'}):
        print(f'{name}: refreshing for Go {expected}', flush=True)
        subprocess.run(['make', f'O={output}', name + '-dirclean'],
                       cwd=repo / 'buildroot', check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--commit', action='store_true')
    args = parser.parse_args()
    refresh(Path(__file__).resolve().parents[2], args.output.resolve(), args.commit)
