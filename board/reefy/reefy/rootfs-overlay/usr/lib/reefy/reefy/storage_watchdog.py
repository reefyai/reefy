"""Independent stale/physical-pressure detection without the Docker API."""
import json
import os
from pathlib import Path
import time

from reefy.storage_pressure import PressureError
from reefy.storage_quota import RUN_DIR, atomic_json, physical_sample


WRITER_GROUPS = (
    'docker.slice',
    'system.slice/docker.service',
    'system.slice/containerd.service',
    'system.slice/reefy-reconciler.service',
    'system.slice/reefy-backup.service',
)


class Writers:
    """Freeze allocating processes while keeping Reefy control/SSH available."""
    def __init__(self, root='/sys/fs/cgroup', groups=WRITER_GROUPS):
        self.root = Path(root)
        self.groups = groups

    def freeze(self, *, timeout=3):
        pending = []
        for group in self.groups:
            directory = self.root / group
            try:
                (directory / 'cgroup.freeze').write_text('1')
                pending.append(directory)
            except FileNotFoundError:
                if directory.exists():
                    raise PressureError('writer cgroup lacks freezer support')
                # A missing unit has no processes. Startup barriers must stop
                # new writer services from appearing while protection is held.
        deadline = time.monotonic() + timeout
        while pending:
            remaining = []
            for directory in pending:
                try:
                    events = dict(line.split() for line in
                                  (directory / 'cgroup.events').read_text().splitlines())
                except FileNotFoundError:
                    if directory.exists():
                        raise PressureError('writer freezer state is unreadable')
                    continue
                if events.get('frozen') != '1':
                    remaining.append(directory)
            pending = remaining
            if pending and time.monotonic() >= deadline:
                raise PressureError('writers did not freeze within the response bound')
            if pending:
                time.sleep(0.02)

    def thaw(self):
        # Only the activation/recovery coordinator may release writers, after
        # inventory, quotas and physical samples have all been revalidated.
        for group in reversed(self.groups):
            path = self.root / group / 'cgroup.freeze'
            try:
                path.write_text('0')
            except FileNotFoundError:
                if path.parent.exists():
                    raise PressureError('writer cgroup disappeared during recovery')


def unhealthy_reason(status, sample, now, *, stale_seconds):
    """A heartbeat in another boot or in the future never proves liveness."""
    try:
        sampled = status['sampled_monotonic']
        completed = status['completed_monotonic']
        ceiling = status['allocation']['boundaries']['state']
        if not 0 <= sampled <= completed <= now:
            return 'invalid guard heartbeat'
        if now - sampled > stale_seconds:
            return 'guard measurements are stale'
        if (not sample.healthy
                or sample.metadata_used * 100 >= sample.metadata_capacity * 85):
            return 'thin-pool health or metadata pressure'
        if sample.used >= ceiling or status['allocation']['quiesce']:
            return 'physical emergency boundary reached'
    except (KeyError, TypeError, ValueError):
        return 'missing guard evidence'
    return None


def check(*, active, stale_seconds, sample=None, writers=None,
          status_path=RUN_DIR + '/status.json', now=None):
    if not active:
        return None
    writers = writers or Writers()
    sample = sample or (lambda: physical_sample(timeout=1))
    try:
        with open(status_path) as source:
            status = json.load(source)
        current = sample()
        now = time.monotonic() if now is None else now
        reason = unhealthy_reason(status, current, now, stale_seconds=stale_seconds)
    except Exception as error:
        reason = f'protection evidence unavailable: {type(error).__name__}'
    if reason:
        # Publish the hold before freezing so service start gates reject new
        # writers even when an existing process cannot freeze promptly.
        atomic_json(RUN_DIR + '/hold.json', {'reason': reason, 'monotonic': now})
        writers.freeze()
    return reason
