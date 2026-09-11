"""Independent stale/physical-pressure detection without the Docker API."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from reefy.storage_pressure import PressureError
from reefy.storage_quota import RUN_DIR, atomic_json, physical_sample, state_lock, command, Registry


# Maximum physical-evidence age; also covers systemd's eight-second observer
# watchdog plus scheduling margin. This and drain time form the physical budget.
DETECTION_SECONDS = 10
QUIESCE_SECONDS = 30


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



class BoundedSampler:
    """One in-flight sample, even when killing its subprocess cannot finish.

    A subprocess in uninterruptible kernel I/O can outlive communicate's
    timeout. Only this daemon worker waits for it; the observer keeps running.
    Later calls neither reuse an expired result nor launch parallel samples.
    The observer is the sole caller; the event publishes the worker's result.
    """
    def __init__(self, sample=None):
        self.sample = sample
        self.pending = None

    def read(self, *, timeout=2):
        pending = self.pending
        if pending is None:
            pending = {'done': threading.Event(),
                       'deadline': time.monotonic() + timeout}
            self.pending = pending

            def work():
                try:
                    # Leave part of the observer budget for process cleanup
                    # after a normal command timeout. A stuck cleanup is still
                    # bounded by the outer event wait.
                    pending['result'] = (self.sample or physical_sample)(timeout=timeout * 0.9)
                except Exception as error:
                    pending['error'] = error
                finally:
                    pending['completed'] = time.monotonic()
                    pending['done'].set()

            try:
                threading.Thread(target=work, name='storage-physical-sample', daemon=True).start()
            except Exception:
                self.pending = None
                raise
        remaining = max(0, pending['deadline'] - time.monotonic())
        if not pending['done'].wait(remaining):
            raise TimeoutError('physical sample worker is still pending')
        self.pending = None
        if (pending['completed'] > pending['deadline']
                or time.monotonic() > pending['deadline']):
            raise TimeoutError('late physical sample is not fresh evidence')
        if 'error' in pending:
            raise pending['error']
        return pending['result']

    def discard_pending(self, *, timeout):
        """Wait within the retry budget for the old worker, then discard it.

        A late result never establishes safety. Waiting for termination is only
        permission to issue a fresh sample without overlapping subprocesses.
        """
        pending = self.pending
        if pending is not None:
            if not pending['done'].wait(max(0, timeout)):
                raise TimeoutError('physical sample worker still prevents a fresh retry')
            self.pending = None


_observer_sampler = BoundedSampler()


def sample_with_retry(*, timeout=2):
    # Each status+table pair and process cleanup share a two-second observer
    # budget. One retry handles transient device-mapper/CPU latency. A worker
    # still blocked after its deadline prevents parallel replacement commands.
    # The freeze request is nonblocking. Sampling stays below systemd's
    # eight-second watchdog while queued I/O drains independently.
    # Only a fresh successful sample authorizes continued writes.
    deadline = time.monotonic() + 2 * timeout
    try:
        return _observer_sampler.read(timeout=timeout)
    except (subprocess.TimeoutExpired, TimeoutError):
        # The outer deadline can expire shortly before child reaping finishes.
        # An immediate second read would just revisit that expired worker,
        # consuming the retry without ever making a fresh measurement.
        _observer_sampler.discard_pending(timeout=deadline - time.monotonic())
        remaining = min(timeout, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError('physical sample retry budget expired')
        return _observer_sampler.read(timeout=remaining)


class PhysicalObserver:
    """Bound transient I/O stalls by the existing ten-second detection budget.

    Only this observer's verified sample can bridge a timeout. An old guard
    heartbeat cannot renew it, and its age starts before the sampling call.
    Known unhealthy results and non-timeout errors are never delayed.
    """
    def __init__(self, sample=None, clock=time.monotonic, max_age=DETECTION_SECONDS):
        self.sample = sample
        self.clock, self.max_age = clock, max_age
        self.reset()

    def reset(self):
        self.latest = self.sampled_at = None

    def read(self):
        started = self.clock()
        remaining = (self.max_age if self.sampled_at is None else
                     self.sampled_at + self.max_age - started)
        if remaining <= 0:
            self.latest = self.sampled_at = None
            raise TimeoutError('physical evidence exceeded the detection window')
        try:
            current = (self.sample or sample_with_retry)(timeout=min(2, remaining / 2))
        except (subprocess.TimeoutExpired, TimeoutError):
            if self.sampled_at is None or self.clock() >= self.sampled_at + self.max_age:
                self.latest = self.sampled_at = None
                raise
            return self.latest
        self.latest, self.sampled_at = current, started
        return current


_physical_observer = PhysicalObserver()


def check(*, active, stale_seconds, sample=None, writers=None,
          status_path=RUN_DIR + '/status.json', now=None):
    if not active:
        _physical_observer.reset()
        return None
    writers = writers or Writers()
    sample = sample or _physical_observer.read
    supplied_now = now
    try:
        with open(status_path) as source:
            status = json.load(source)
        current = sample()
        now = time.monotonic() if now is None else now
        reason = unhealthy_reason(status, current, now, stale_seconds=stale_seconds)
    except Exception as error:
        reason = f'protection evidence unavailable: {type(error).__name__}'
    return hold_writers(reason, writers=writers, now=supplied_now)


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
    path = RUN_DIR + '/hold.json'
    with hold_lock():
        now = time.monotonic() if now is None else now
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
        if not hold.get('drained') and now - started > QUIESCE_SECONDS:
            changed = changed or not hold.get('deadline_exceeded')
            hold['deadline_exceeded'] = True
        hold['frozen'] = frozen
        if changed or hold.get('deadline_exceeded'):
            atomic_json(path, hold)
        if hold.get('deadline_exceeded'):
            return 'writer quiescence exceeded its physical response bound'
        if hold.get('drain_error'):
            return 'filesystem drain failed; storage writers remain held'
        return hold.get('reason', 'storage writers held')


def start_hold_worker():
    """Queue one independent drain worker for this hold, without waiting on I/O."""
    path = RUN_DIR + '/hold.json'
    with hold_lock():
        try:
            with open(path) as source:
                hold = json.load(source)
        except FileNotFoundError:
            return
        if hold.get('drained') or hold.get('worker_requested') or hold.get('recovering'):
            return
        # Serialize the bounded, nonblocking start request with recovery. Once
        # recovery marks this epoch, no late queued worker can refreeze apps.
        command(['systemctl', 'start', '--no-block', 'reefy-storage-drain.service'], timeout=1)
        hold['worker_requested'] = True
        atomic_json(path, hold)


def finish_hold(*, writers=None, mounts=None):
    """Confirm freezing AND flush queued filesystem work inside one deadline.

    This runs in a separate systemd worker, outside the writer cgroups. It never
    holds the coordination lock while waiting for the freezer or filesystem I/O.
    Explicit mount selection is used only by the disposable kernel probes.
    """
    writers = writers or Writers()
    path = RUN_DIR + '/hold.json'
    with hold_lock():
        try:
            with open(path) as source:
                hold = json.load(source)
        except FileNotFoundError:
            return  # A previously queued start arrived after recovery.
        if hold.get('recovering'):
            return
        epoch = hold['monotonic']
        if hold.get('drained'):
            return
        hold['worker_requested'] = True
        atomic_json(path, hold)
    deadline = epoch + QUIESCE_SECONDS
    try:
        writers.freeze(timeout=max(0, deadline - time.monotonic()))
        frozen_at = time.monotonic()
        if mounts is None:
            registry = Registry()
            if not registry.data.get('inventory_complete'):
                raise PressureError('cannot drain an incomplete writer inventory')
            mounts = sorted({r['mount'] for r in registry.data['projects'].values()
                             if not r.get('retired')})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PressureError('no physical response time remains for filesystem work')
        command([sys.executable, '-c',
                 'import sys; sys.path.insert(0, "/usr/lib/reefy"); '
                 'from reefy.storage_quota import flush_filesystem; '
                 '[flush_filesystem(path) for path in sys.argv[1:]]', *mounts],
                timeout=remaining)
        drained_at = time.monotonic()
        with hold_lock():
            with open(path) as source:
                hold = json.load(source)
            if hold['monotonic'] != epoch:
                raise PressureError('storage hold changed during drain')
            hold.update(frozen=True, drained=True, frozen_monotonic=frozen_at,
                        drained_monotonic=drained_at)
            if drained_at > deadline:
                hold['deadline_exceeded'] = True
            atomic_json(path, hold)
            if hold.get('deadline_exceeded'):
                raise PressureError('filesystem drain missed the physical response bound')
    except Exception as error:
        with hold_lock():
            with open(path) as source:
                hold = json.load(source)
            if hold['monotonic'] == epoch:
                hold['drain_error'] = type(error).__name__
                if time.monotonic() > deadline:
                    hold['deadline_exceeded'] = True
                atomic_json(path, hold)
        raise
