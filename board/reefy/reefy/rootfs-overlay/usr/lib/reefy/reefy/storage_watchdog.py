"""Independent stale/physical-pressure detection without the Docker API."""
import json
import os
from pathlib import Path
import subprocess
import time

from reefy.storage_pressure import PressureError
from reefy.storage_quota import RUN_DIR, atomic_json, physical_sample, state_lock


# Eight seconds for systemd to detect a stuck observer, plus scheduling margin.
DETECTION_SECONDS = 10
FREEZE_SECONDS = 25
DRAIN_SECONDS = 5


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

    def request_freeze(self):
        for group in self.groups:
            directory = self.root / group
            try:
                (directory / 'cgroup.freeze').write_text('1')
            except FileNotFoundError:
                if directory.exists():
                    raise PressureError('writer cgroup lacks freezer support')
                # Startup barriers prevent new units while the hold exists.

    def frozen(self):
        for group in self.groups:
            directory = self.root / group
            try:
                events = dict(line.split() for line in
                              (directory / 'cgroup.events').read_text().splitlines())
            except FileNotFoundError:
                if directory.exists():
                    raise PressureError('writer freezer state is unreadable')
                continue
            if events.get('frozen') != '1':
                return False
        return True

    def freeze(self, *, timeout=3):
        """Bounded synchronous barrier for explicit recovery, not the observer."""
        self.request_freeze()
        deadline = time.monotonic() + timeout
        while not self.frozen():
            if time.monotonic() >= deadline:
                raise PressureError('writers have not finished freezing')
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
        stop_at = status.get('physical_stop_bytes', ceiling)
        if type(stop_at) is not int or not 0 <= stop_at <= ceiling:
            return 'invalid physical containment threshold'
        if not 0 <= sampled <= completed <= now:
            return 'invalid guard heartbeat'
        if now - sampled > stale_seconds:
            return 'guard measurements are stale'
        if (not sample.healthy
                or sample.metadata_used * 100 >= sample.metadata_capacity * 85):
            return 'thin-pool health or metadata pressure'
        if sample.used >= stop_at or status['allocation']['quiesce']:
            return 'physical emergency boundary reached'
    except (KeyError, TypeError, ValueError):
        return 'missing guard evidence'
    return None



def sample_with_retry():
    # Each complete status+table pair shares a two-second deadline. One retry
    # handles transient device-mapper/CPU latency during image extraction.
    # The freeze request is nonblocking. Sampling stays below systemd's
    # eight-second watchdog while queued I/O drains independently.
    # Only a fresh successful sample authorizes continued writes.
    try:
        return physical_sample(timeout=2)
    except (subprocess.TimeoutExpired, TimeoutError):
        return physical_sample(timeout=2)


def check(*, active, stale_seconds, sample=None, writers=None,
          status_path=RUN_DIR + '/status.json', now=None):
    if not active:
        return None
    writers = writers or Writers()
    sample = sample or sample_with_retry
    try:
        with open(status_path) as source:
            status = json.load(source)
        current = sample()
        now = time.monotonic() if now is None else now
        reason = unhealthy_reason(status, current, now, stale_seconds=stale_seconds)
    except Exception as error:
        reason = f'protection evidence unavailable: {type(error).__name__}'
    return hold_writers(reason, writers=writers, now=now)


def hold_lock():
    # Never hold this lock while sampling, flushing filesystems or reading LVM.
    return state_lock(RUN_DIR + '/hold.lock', timeout=1)


def hold_writers(reason=None, *, writers=None, now=None):
    """Latch a hold and observe completion without blocking the fast observer.

    A freeze request stops future userspace execution, but cannot cancel I/O
    already in the kernel. Completion may take seconds under COW pressure.
    The first deadline survives repeated checks and watchdog restarts. A missed
    deadline remains a failed response bound, even if the kernel later drains.
    """
    writers = writers or Writers()
    now = time.monotonic() if now is None else now
    path = RUN_DIR + '/hold.json'
    with hold_lock():
        try:
            with open(path) as source:
                hold = json.load(source)
        except FileNotFoundError:
            hold = None
        except (OSError, json.JSONDecodeError):
            hold = {'reason': 'storage hold evidence is unreadable',
                    'monotonic': now, 'deadline_exceeded': True}
        if hold is None:
            if reason is None:
                return None
            hold = {'reason': reason, 'monotonic': now}
            # Publish the startup barrier before touching any writer cgroup.
            atomic_json(path, hold)
        if not isinstance(hold, dict):
            hold = {'reason': 'storage hold evidence is invalid',
                    'monotonic': now, 'deadline_exceeded': True}
        started = hold.get('monotonic')
        if type(started) not in (int, float) or not 0 <= started <= now:
            # Unknown elapsed time cannot establish a valid response deadline.
            hold['deadline_exceeded'] = True
            hold['monotonic'] = now
            started = now
        writers.request_freeze()
        frozen = bool(writers.frozen())
        changed = hold.get('frozen') != frozen
        if hold.get('frozen') is not True and now - started > FREEZE_SECONDS:
            changed = changed or not hold.get('deadline_exceeded')
            hold['deadline_exceeded'] = True
        hold['frozen'] = frozen
        if changed or hold.get('deadline_exceeded'):
            atomic_json(path, hold)
        if hold.get('deadline_exceeded'):
            return 'writer quiescence exceeded its physical response bound'
        return hold.get('reason', 'storage writers held')
