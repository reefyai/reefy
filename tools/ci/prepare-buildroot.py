#!/usr/bin/env python3
"""Apply the verbatim upstream Go security backport to pinned stable Buildroot."""
import hashlib
from pathlib import Path
import subprocess

REVISION = 'd6061d653d7fb215c2409c59e4bfad1b9d677215'
PATCH_SHA256 = 'bf8c43c4dd774807071272ff1301c20bcf00f0e4653c5ac6efa9264a2c00547a'


def apply_backport(tree, patch):
    if hashlib.sha256(patch.read_bytes()).hexdigest() != PATCH_SHA256:
        raise RuntimeError('upstream Go patch checksum mismatch')
    command = ['git', 'apply', '--check', str(patch)]
    if subprocess.run(command, cwd=tree, capture_output=True).returncode == 0:
        subprocess.run(['git', 'apply', str(patch)], cwd=tree, check=True)
        return
    if subprocess.run(['git', 'apply', '--reverse', '--check', str(patch)],
                      cwd=tree, capture_output=True).returncode == 0:
        return
    raise RuntimeError('Go backport neither applies cleanly nor is already applied')


def prepare(repo):
    tree = repo / 'buildroot'
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=tree, text=True).strip()
    if actual != REVISION:
        raise RuntimeError(f'Buildroot revision mismatch: expected {REVISION}, got {actual}')
    apply_backport(tree, repo / 'support/buildroot/0001-go-security-1.27.3.patch')


if __name__ == '__main__':
    prepare(Path(__file__).resolve().parents[2])
