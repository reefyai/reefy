"""Bulk-only pressure targets. Quotas are not physical block reservations."""
from dataclasses import dataclass

GB = 1_000_000_000
QUANTUM = 4096
INTERVAL = 60
SAMPLE_MAX_AGE = 10


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class Pool:
    capacity: int
    used: int
    metadata_used: int
    metadata_capacity: int
    chunk_bytes: int
    healthy: bool

    def __post_init__(self):
        if (self.capacity <= 0 or not 0 <= self.used <= self.capacity
                or self.metadata_capacity <= 0
                or not 0 <= self.metadata_used <= self.metadata_capacity
                or self.chunk_bytes <= 0):
            raise StorageError('invalid physical pool counters')


def parse_thin_sample(status, table):
    try:
        if len(status.splitlines()) != 1 or len(table.splitlines()) != 1:
            raise ValueError('expected a single thin pool')
        status, table = status.split(), table.split()
        si, ti = status.index('thin-pool'), table.index('thin-pool')
        metadata = [int(v) for v in status[si + 2].split('/')]
        data = [int(v) for v in status[si + 3].split('/')]
        chunk = int(table[ti + 3]) * 512
        healthy = ('rw' in status[si + 5:] and not set(status).intersection(
            {'Fail', 'Error', 'ro', 'out_of_data_space', 'needs_check'}))
        return Pool(data[1] * chunk, data[0] * chunk,
                    metadata[0] * 4096, metadata[1] * 4096, chunk, healthy)
    except (ValueError, IndexError) as error:
        raise StorageError('unreadable thin pool counters') from error


def headroom(capacity):
    """Initial internal policy: 20%, at least 4 GB and at most 200 GB."""
    return min(max((capacity + 4) // 5, 4 * GB), 200 * GB)


def targets(pool, usage):
    """One device-wide budget, split only where filesystems require it.

    On one filesystem all bulk roots share one project. Across filesystems,
    distribute spare growth equally and reductions proportionally to usage.
    This does not promise instantaneous sharing across filesystem boundaries.
    Zero is never a quota because XFS interprets it as unlimited.
    """
    if not pool.healthy:
        raise StorageError('thin pool is unhealthy')
    if not usage:
        return {}
    if any(type(v) is not int or v < 0 for v in usage.values()):
        raise ValueError('invalid project usage')
    floor = min(pool.capacity // 100, GB) // QUANTUM * QUANTUM
    if floor < len(usage) * QUANTUM or headroom(pool.capacity) >= pool.capacity:
        raise StorageError('physical pool is too small for bulk headroom')
    total = sum(usage.values())
    budget = max(floor, total + pool.capacity - pool.used - headroom(pool.capacity))
    # Metadata is finite independently of data. Give bulk no new allocations
    # at critical metadata pressure; do not pause any writers.
    if pool.metadata_used * 10 >= pool.metadata_capacity * 9:
        budget = floor
    budget = budget // QUANTUM * QUANTUM
    keys = sorted(usage)
    units = budget // QUANTUM
    result = {key: 1 for key in keys}
    units -= len(keys)
    if budget >= total:
        # Preserve occupied bytes, including a nonzero limit for empty roots.
        base = {key: max(1, (usage[key] + QUANTUM - 1) // QUANTUM)
                for key in keys}
        if sum(base.values()) <= budget // QUANTUM:
            result = base
            units = budget // QUANTUM - sum(base.values())
            share, remainder = divmod(units, len(keys))
            return {key: (result[key] + share + (i < remainder)) * QUANTUM
                    for i, key in enumerate(keys)}
    # Below usage: proportional total limits, not usage plus a free-space floor.
    assigned = 0
    for key in keys:
        extra = units * usage[key] // max(total, 1)
        result[key] += extra
        assigned += extra
    for key in keys[:units - assigned]:
        result[key] += 1
    return {key: value * QUANTUM for key, value in result.items()}


def apply_targets(current, desired, write, read, *, allow_growth=True):
    """Verify every reduction before any increase; unknown quota is unlimited."""
    for key in sorted(desired):
        old, new = current[key], desired[key]
        if old == 0 or new < old:
            write(key, new)
            if read(key) != new:
                raise StorageError('bulk quota reduction verification failed')
    if allow_growth:
        for key in sorted(desired):
            if current[key] and desired[key] > current[key]:
                write(key, desired[key])
                if read(key) != desired[key]:
                    raise StorageError('bulk quota increase verification failed')
