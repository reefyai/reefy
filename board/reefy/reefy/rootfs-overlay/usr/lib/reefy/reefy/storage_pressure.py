"""Physical-pool budgeting, independent of Linux commands and app packages.

Limits are outstanding allocation allowances, not reservations of physical
blocks. The runtime must revoke and verify old allowances before granting new
ones and monitor COW/metadata allocation independently of project usage.
"""

from dataclasses import dataclass, replace
from math import ceil, isfinite


GB = 1_000_000_000
MIB = 1024 * 1024
# Reefy x86_64 XFS uses 4 KiB filesystem blocks. Smaller requests are
# rounded by the kernel, so they cannot be used as verified hard limits.
QUANTUM = 4096
CLASSES = ('bulk', 'runtime', 'state')


class PressureError(RuntimeError):
    """Storage cannot safely accept new allocation."""


@dataclass(frozen=True)
class PoolSample:
    capacity: int
    used: int
    metadata_used: int
    metadata_capacity: int
    chunk_bytes: int
    healthy: bool = True

    def __post_init__(self):
        if (self.capacity <= 0 or not 0 <= self.used <= self.capacity
                or self.metadata_capacity <= 0
                or not 0 <= self.metadata_used <= self.metadata_capacity
                or self.chunk_bytes <= 0):
            raise ValueError('invalid physical-pool sample')


@dataclass(frozen=True)
class Boundaries:
    bulk: int
    runtime: int
    state: int
    emergency: int


def boundaries(capacity, *, peak_bytes_per_second=0, response_seconds=30,
               in_flight_bytes=0):
    """Use actual thin data capacity, with upward-rounded safety margins."""
    if (not isinstance(capacity, int) or isinstance(capacity, bool)
            or capacity <= 0 or not isfinite(peak_bytes_per_second)
            or peak_bytes_per_second < 0 or response_seconds <= 0
            or in_flight_bytes < 0):
        raise ValueError('invalid capacity or response bound')
    headroom = min((capacity + 9) // 10, 100 * GB)
    base = min(max((capacity + 19) // 20, 4 * GB), 50 * GB)
    emergency = max(base, ceil(peak_bytes_per_second * response_seconds)
                    + in_flight_bytes)
    state = capacity - emergency
    runtime = state - headroom
    bulk = runtime - headroom
    if bulk <= 0:
        raise PressureError('insufficient capacity for storage headroom')
    return Boundaries(bulk, runtime, state, emergency)


@dataclass(frozen=True)
class Consumer:
    key: str
    storage_class: str
    used: int
    hard: int
    demand: int = 0  # recent positive allocation rate, bytes/second
    max_hard: int | None = None  # an independently configured limit
    minimum_hard: int = 0  # internal control-state runway, never app policy

    def __post_init__(self):
        if (not self.key or self.storage_class not in CLASSES
                or min(self.used, self.hard, self.demand) < 0
                or (self.max_hard is not None and self.max_hard <= 0)
                or type(self.minimum_hard) is not int or self.minimum_hard < 0
                or self.minimum_hard % QUANTUM
                or (self.max_hard is not None and self.minimum_hard > self.max_hard)):
            raise ValueError('invalid quota consumer')


@dataclass(frozen=True)
class Allocation:
    limits: dict[str, int]
    boundaries: Boundaries
    stage: str
    granted: int
    quiesce: bool


def _charged_floor(used):
    # Zero means unlimited to XFS, including an empty project at pressure.
    return max(QUANTUM, ((used + QUANTUM - 1) // QUANTUM) * QUANTUM)


def allocate(sample, consumers, *, pending_bytes=0, peak_bytes_per_second=0,
             response_seconds=30, in_flight_bytes=0):
    """Plan all grants against ONE boundary, including higher-priority writers.

    Near a lower boundary, first remove that class's allowance, then open the
    next band for the remaining writers. Existing higher-priority usage counts
    in the same physical sample. Deletions do not refund space until the thin
    sample observes reclamation. Pure planning does not authorize mutations.
    """
    if pending_bytes < 0:
        raise ValueError('negative pending allocation')
    consumers = tuple(consumers)
    if len({c.key for c in consumers}) != len(consumers):
        raise ValueError('duplicate consumer')
    limits = {c.key: max(_charged_floor(c.used), c.minimum_hard) for c in consumers}
    protected = sum(limits[c.key] - _charged_floor(c.used) for c in consumers)
    bound = boundaries(sample.capacity,
                       peak_bytes_per_second=peak_bytes_per_second,
                       response_seconds=response_seconds,
                       in_flight_bytes=in_flight_bytes)
    # Cover empty-project floors and quota rounding at thin-chunk granularity.
    rounding = sum(sample.chunk_bytes for c in consumers
                   if limits[c.key] > c.used)
    used = sample.used + pending_bytes + protected + rounding
    margin = max(sample.chunk_bytes, ceil(peak_bytes_per_second * response_seconds))
    unsafe = (not sample.healthy
              or sample.metadata_used * 100 >= sample.metadata_capacity * 85
              or used + margin >= bound.state)
    if unsafe:
        return Allocation(limits, bound, 'emergency', 0, True)
    # Do not require writers to consume the final byte of a band. Per-project
    # fragments, filesystem reservations and idle housekeeping quotas can leave
    # less than one application write available indefinitely. Handoff closes
    # the lower class before opening the next band, within one startup window.
    transition = max(64 * MIB, sample.chunk_bytes)
    if used + margin + transition >= bound.runtime:
        stage, allowed, ceiling = 'state', {'state'}, bound.state
    elif used + margin + transition >= bound.bulk:
        stage, allowed, ceiling = 'runtime', {'runtime', 'state'}, bound.runtime
    else:
        stage, allowed, ceiling = 'bulk', set(CLASSES), bound.bulk
    available = max(0, ceiling - used - margin)
    available -= available % QUANTUM
    eligible = [c for c in consumers if c.storage_class in allowed]
    rooms = {c.key: max(0, (c.max_hard - limits[c.key]) // QUANTUM)
             if c.max_hard is not None else available // QUANTUM
             for c in eligible}
    grants = dict.fromkeys(rooms, 0)
    remaining = available // QUANTUM
    # Idle startup grants are bounded. The remainder follows observed demand,
    # giving sustained media writers hours of runway while physical space exists.
    if eligible:
        startup = min(64 * MIB // QUANTUM, remaining // len(eligible))
        for c in eligible:
            amount = min(startup, rooms[c.key])
            grants[c.key] += amount
            remaining -= amount
    active = [c for c in eligible if c.demand > 0]
    if not active:
        active = eligible  # bounded by the same shared budget during cold start
    while remaining and active:
        weights = sum(max(1, c.demand) for c in active)
        initial = remaining
        exhausted = []
        for c in active:
            amount = min(rooms[c.key] - grants[c.key], remaining,
                         max(1, initial * max(1, c.demand) // weights))
            grants[c.key] += amount
            remaining -= amount
            if grants[c.key] == rooms[c.key]:
                exhausted.append(c.key)
        active = [c for c in active if c.key not in exhausted]
        if remaining == initial:
            break
    for key, value in grants.items():
        limits[key] += value * QUANTUM
    return Allocation(limits, bound, stage, sum(grants.values()) * QUANTUM, False)


def apply_allocation(consumers, allocation, set_limit, read_limit):
    """Revoke and read back every reduction before ANY increase.

    Unknown/unlimited initial limits also require establishment in the revoke
    phase. If a tool errors or verification disagrees, no grant phase runs.
    """
    consumers = tuple(consumers)
    established = {}
    for c in consumers:
        target = allocation.limits[c.key]
        if target <= 0:
            raise PressureError('refusing unlimited project quota')
        if c.hard == 0 or target < c.hard:
            reduced = min(target, _charged_floor(c.used)) if c.hard == 0 else target
            set_limit(c.key, reduced)
            if read_limit(c.key) != reduced:
                raise PressureError('quota revocation verification failed')
            established[c.key] = reduced
    for c in consumers:
        target = allocation.limits[c.key]
        if target > established.get(c.key, c.hard):
            set_limit(c.key, target)
            if read_limit(c.key) != target:
                raise PressureError('quota grant verification failed')


def parse_thin_sample(status, table):
    """Read exact dm-thin block counters, never rounded lvs percentages."""
    status = status.split()
    table = table.split()
    try:
        si, ti = status.index('thin-pool'), table.index('thin-pool')
        metadata = [int(v) for v in status[si + 2].split('/')]
        data = [int(v) for v in status[si + 3].split('/')]
        chunk = int(table[ti + 3]) * 512
        healthy = ('rw' in status[si + 5:]
                   and not set(status).intersection({
                       'Fail', 'Error', 'ro', 'out_of_data_space', 'needs_check'}))
        return PoolSample(data[1] * chunk, data[0] * chunk,
                          metadata[0] * 4096, metadata[1] * 4096,
                          chunk, healthy)
    except (ValueError, IndexError) as error:
        raise PressureError('unreadable thin-pool counters') from error


def admit_reservations(allocation, sample, consumers, leases, *, margin):
    """Attach already-budgeted targeted allowances after class admission.

    allocate() must first subtract the sum of ALL lease bytes as pending_bytes.
    Untargeted leases retain that reservation without increasing any quota.
    """
    if allocation.quiesce:
        return allocation, []
    consumers = {consumer.key: consumer for consumer in consumers}
    total = sample.used + sum(lease['bytes'] for lease in leases.values()) + margin
    admitted, extra = [], {}
    for identity, lease in leases.items():
        ceiling = getattr(allocation.boundaries, lease['storage_class'])
        if total >= ceiling:
            continue
        target = lease.get('target')
        if target:
            consumer = consumers.get(target)
            if consumer is None or consumer.storage_class != lease['storage_class']:
                raise PressureError('admission destination or storage class changed')
            amount = extra.get(target, 0) + lease['bytes']
            if (consumer.max_hard is not None
                    and allocation.limits[target] + amount > consumer.max_hard):
                continue
            extra[target] = amount
        admitted.append(identity)
    limits = dict(allocation.limits)
    for target, amount in extra.items():
        limits[target] += amount
    return replace(allocation, limits=limits,
                   granted=allocation.granted + sum(extra.values())), admitted
