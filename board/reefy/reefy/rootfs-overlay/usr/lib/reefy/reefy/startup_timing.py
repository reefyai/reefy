"""Boot-relative, secret-free component spans shared by startup processes.

Only intervals where every active leaf is downloading or waiting on another
leaf are eligible for exclusion. Concurrent local work always keeps the clock
running. Unknown, failed or incomplete instrumentation never earns credit.
"""
from contextlib import contextmanager
from functools import wraps
import fcntl
import json
import math
import os
import re
import threading
import time
import uuid

PATH = '/run/reefy/startup-timings.jsonl'
MAX_BYTES = 16 * 1024 * 1024
PARENT_ENV = '_REEFY_TIMING_PARENT'
try:
    with open('/proc/sys/kernel/random/boot_id') as _boot_file:
        BOOT_ID = _boot_file.read().strip()
except OSError:
    BOOT_ID = 'non-device-test-process'
_local = threading.local()
LABELS = frozenset({
    'reconciliation', 'applications', 'application', 'dispatch',
    'dependency_wait', 'artifact_preparation', 'artifact_download',
    'artifact_verification', 'artifact_mount', 'provider_activation',
    'cached_providers', 'image_pull', 'image_download', 'container_start',
    'storage', 'wifi', 'network', 'files', 'users', 'backup',
})


def seal():
    """Retain first-ready evidence; already-started spans may still close."""
    try:
        os.makedirs(os.path.dirname(PATH), mode=0o755, exist_ok=True)
        fd = os.open(PATH+'.ready', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(BOOT_ID)
    except OSError:
        pass


def current():
    return getattr(_local, 'span', None)


def _emit(record):
    # Observability must not break device reconciliation. Missing/truncated
    # records are detected by the collector and receive no download credit.
    try:
        os.makedirs(os.path.dirname(PATH), mode=0o755, exist_ok=True)
        fd = os.open(PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            if (record.get('event') == 'start' and os.path.exists(PATH+'.ready')
                    or os.fstat(stream.fileno()).st_size >= MAX_BYTES):
                return False
            stream.write(json.dumps({'boot_id': BOOT_ID, **record}, separators=(',', ':')) + '\n')
            stream.flush()
            return True
    except OSError:
        return False


class Span:
    def __init__(self, component, kind='local', parent=None):
        if component not in LABELS or kind not in ('local', 'wait', 'download'):
            raise ValueError('invalid internal timing label')
        self.id = uuid.uuid4().hex
        self.component, self.kind = component, kind
        self.parent = parent
        self.start = time.monotonic()
        self.closed = False
        self.active_at = None
        self.recorded = _emit({'event': 'start', 'id': self.id, 'parent': parent,
               'component': component, 'kind': kind, 'at': self.start})

    def close(self, succeeded=True):
        if not self.closed:
            self.closed = True
            if not self.recorded:
                return
            _emit({'event': 'end', 'id': self.id, 'at': time.monotonic(),
                   'active_at': self.active_at if self.active_at is not None else self.start,
                   'succeeded': bool(succeeded)})


@contextmanager
def scope(component, kind='local', parent=None, root=False):
    previous = current()
    parent = parent if parent is not None else previous
    if parent is None and not root:
        yield None
        return
    span = Span(component, kind, parent)
    _local.span = span.id
    try:
        if kind == 'download':
            span.active_at = time.monotonic()
        yield span
    except BaseException:
        span.close(False)
        raise
    else:
        span.close()
    finally:
        _local.span = previous


def component(label, kind='local', root=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with scope(label, kind, root=root):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def submit(pool, fn, *args, **kwargs):
    """Record queued work before dispatch so it cannot look like idle wait."""
    parent = current()
    if parent is None:
        return pool.submit(fn, *args, **kwargs)
    with scope('dispatch'):
        span = Span('application', parent=parent)
        def run():
            previous = current()
            _local.span = span.id
            try:
                result = fn(*args, **kwargs)
            except BaseException:
                span.close(False)
                raise
            else:
                span.close()
                return result
            finally:
                _local.span = previous
        try:
            return pool.submit(run)
        except BaseException:
            span.close(False)
            raise


def subprocess_env():
    environment = os.environ.copy()
    if current():
        environment[PARENT_ENV] = current()
    else:
        environment.pop(PARENT_ENV, None)
    return environment


def inherited_parent():
    value = os.environ.get(PARENT_ENV, '')
    return value if re.fullmatch(r'[0-9a-f]{32}', value) else None


class ImageDownloads:
    """Conservative progress evidence, never the whole compose-pull duration.

    Checksum/verification/extraction/waiting/unknown layer states keep counting.
    Credit is bounded by two observed Downloading updates, so buffered final
    status and unreported registry/manifest wait cannot extend an exclusion.
    """
    def __init__(self):
        self.parent = current()
        self.states = {}
        self.begin = self.last = None
        self.intervals = []

    def feed(self, line):
        match = re.search(
            r'\b([0-9a-f]{12,64})\s+(Downloading|Extracting|Verifying Checksum|'
            r'Download complete|Pull complete|Already exists|Waiting|Pulling fs layer)\b', line)
        if not match:
            self._finish_interval()
            return
        layer, status = match.groups()
        at = time.monotonic()
        if status in ('Pull complete', 'Already exists'):
            self.states.pop(layer, None)
        else:
            self.states[layer] = status
        downloading_only = bool(self.states) and all(
            value == 'Downloading' for value in self.states.values())
        if downloading_only and status == 'Downloading':
            if self.begin is None:
                self.begin = at
            self.last = at
        else:
            self._finish_interval()

    def _finish_interval(self):
        if self.begin is not None and self.last > self.begin:
            self.intervals.append((self.begin, self.last))
        self.begin = self.last = None

    def close(self, succeeded):
        self._finish_interval()
        if not succeeded or not self.parent or self.states:
            return
        for start, end in self.intervals:
            identifier = uuid.uuid4().hex
            if _emit({'event': 'start', 'id': identifier, 'parent': self.parent,
                      'component': 'image_download', 'kind': 'download', 'at': start}):
                _emit({'event': 'end', 'id': identifier, 'at': end, 'succeeded': True})


def summarize(records, first_ready_s):
    """Partition elapsed time; overlapping spans are never added together."""
    if isinstance(first_ready_s, bool) or not isinstance(first_ready_s, (int, float)) or not math.isfinite(first_ready_s) or first_ready_s <= 0:
        raise ValueError('invalid first ready timestamp')
    starts, ends = {}, {}
    for row in records:
        if not isinstance(row, dict):
            raise ValueError('invalid timing record')
        if row.get('event') not in ('start', 'end'):
            raise ValueError('unknown timing record')
        key, at = row.get('id'), row.get('at')
        if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{32}', key):
            raise ValueError('invalid span identity')
        if not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at) or at < 0:
            raise ValueError('invalid timing timestamp')
        table = starts if row['event'] == 'start' else ends
        if key in table:
            raise ValueError('duplicate timing record')
        table[key] = row
    if not starts or set(ends) - set(starts):
        raise ValueError('missing timing starts')
    spans = {}
    for key, row in starts.items():
        if row['at'] >= first_ready_s:
            continue
        if row.get('component') not in LABELS or row.get('kind') not in ('local', 'wait', 'download'):
            raise ValueError('invalid timing label')
        end = ends.get(key)
        # Any truncated/open span before first ready makes exclusion unsafe.
        if end is None or end['at'] < row['at']:
            raise ValueError('incomplete timing evidence')
        active_at = end.get('active_at', row['at'])
        if (not isinstance(active_at, (int, float)) or isinstance(active_at, bool)
                or not math.isfinite(active_at) or not row['at'] <= active_at <= end['at']):
            raise ValueError('invalid download activity boundary')
        if active_at >= first_ready_s:
            continue
        spans[key] = {**row, 'at': active_at, 'end': min(end['at'], first_ready_s),
                      'kind': row['kind'] if end.get('succeeded') is True else 'local'}
    for key, span in spans.items():
        parent = span.get('parent')
        seen = {key}
        while parent is not None:
            if parent not in spans or parent in seen:
                raise ValueError('invalid span ancestry')
            seen.add(parent)
            ancestor = spans[parent]
            if ancestor['at'] > span['at'] or ancestor['end'] < span['end']:
                raise ValueError('child outside parent span')
            parent = ancestor.get('parent')
    # Failed operations and their descendants never receive download credit.
    failed = {key for key in spans if ends[key].get('succeeded') is not True}
    for span in spans.values():
        ancestor = span.get('parent')
        while ancestor is not None:
            if ancestor in failed:
                span['kind'] = 'local'
                break
            ancestor = spans[ancestor].get('parent')
    events = {0.0: [], float(first_ready_s): []}
    for key, span in spans.items():
        events.setdefault(span['at'], []).append((True, key))
        events.setdefault(span['end'], []).append((False, key))
    boundaries = sorted(events)
    active, parts = {}, {}
    for begin, end in zip(boundaries, boundaries[1:]):
        # Zero-length spans must not remain active.
        for entering, key in sorted(events[begin], reverse=True):
            if entering:
                active[key] = spans[key]
            else:
                active.pop(key, None)
        parents = {s['parent'] for s in active.values()}
        leaves = [s for k, s in active.items() if k not in parents]
        local = [s for s in leaves if s['kind'] == 'local']
        if leaves and not local and any(s['kind'] == 'download' for s in leaves):
            label = 'download_only_wait'
        elif local:
            labels = {s['component'] for s in local}
            label = next(iter(labels)) if len(labels) == 1 else 'concurrent_local_work'
        else:
            label = 'unattributed_startup'
        parts[label] = parts.get(label, 0.0) + end - begin
    excluded = parts.get('download_only_wait', 0.0)
    return {'status': 'complete', 'raw_ready_s': first_ready_s,
            'download_only_wait_s': excluded,
            'adjusted_ready_s': first_ready_s - excluded,
            'components_s': parts}


def collect(first_ready_s):
    try:
        with open(PATH) as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            content = stream.read(MAX_BYTES + 1)
        if len(content) >= MAX_BYTES - 4096:
            raise ValueError('timing file exceeds bound')
        records = [json.loads(line) for line in content.splitlines()]
        if any(not isinstance(row, dict) or row.get('boot_id') != BOOT_ID for row in records):
            raise ValueError('timing evidence belongs to another boot')
        return summarize(records, first_ready_s)
    except (OSError, ValueError, TypeError, KeyError):
        return {'status': 'unavailable', 'raw_ready_s': first_ready_s,
                'download_only_wait_s': 0.0, 'adjusted_ready_s': first_ready_s,
                'components_s': {'unattributed_startup': first_ready_s}}
