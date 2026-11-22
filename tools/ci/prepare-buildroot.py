#!/usr/bin/env python3
"""Verify the exact stable Buildroot snapshot without patching its recipes."""
from pathlib import Path
import subprocess

REVISION = 'bbbdab36565d4941b6b0449a0eedb5aac8a10c2e'


def prepare(repo):
    tree = repo / 'buildroot'
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=tree, text=True).strip()
    if actual != REVISION:
        raise RuntimeError(f'Buildroot revision mismatch: expected {REVISION}, got {actual}')
    if subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], cwd=tree).returncode != 0:
        raise RuntimeError('Pinned Buildroot has tracked changes; refusing unreviewed recipes')


if __name__ == '__main__':
    prepare(Path(__file__).resolve().parents[2])
