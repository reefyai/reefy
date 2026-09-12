"""Durable restart-policy enforcement for Compose app lifecycle operations.

Docker may restart manually stopped on-failure containers at daemon startup
when their saved exit code is nonzero. Disable the persisted policy before
Stop, then restore the original Compose policy after a successful Start.
Container filesystems and the desired Compose document are left unchanged.
"""

import re
import subprocess

from reefy.shared import redact_log_message


def _docker(args):
    try:
        result = subprocess.run(['docker', *args], capture_output=True,
                                text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('restart policy operation failed: '
                           + type(error).__name__) from error
    if result.returncode:
        raise RuntimeError('restart policy operation failed: '
                           + redact_log_message(result.stderr or result.stdout)[-1000:])
    return result.stdout


def set_policy(project, policy, service=None):
    """Update only containers belonging to the exact project/service labels."""
    if not project:
        raise ValueError('restart policy requires a project')
    args = ['ps', '--all', '--quiet', '--no-trunc', '--filter',
            'label=com.docker.compose.project=' + project]
    if service is not None:
        if not service:
            raise ValueError('restart policy requires a nonempty service')
        args += ['--filter', 'label=com.docker.compose.service=' + service]
    ids = _docker(args).split()
    if any(not re.fullmatch(r'[0-9a-f]{64}', cid) for cid in ids):
        raise RuntimeError('invalid container ID in restart policy inventory')
    for offset in range(0, len(ids), 100):
        _docker(['update', '--restart=' + policy, *ids[offset:offset + 100]])


def restore_policies(project, compose, services):
    """Restore selected services only, preserving init/no and retry counts."""
    definitions = compose.get('services') or {}
    for name in sorted(set(services or definitions)):
        if name in definitions:
            set_policy(project, definitions[name].get('restart') or 'no', name)
