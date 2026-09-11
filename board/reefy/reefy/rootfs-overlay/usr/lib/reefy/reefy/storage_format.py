"""Reserve physical initialization space before formatting a managed thin LV."""
from contextlib import contextmanager
import re

from reefy.storage_admission import reservation
from reefy.storage_pressure import PressureError, QUANTUM
from reefy.storage_quota import Registry, RUN_DIR, atomic_json, command, physical_sample


def format_budget(layout, chunk_bytes):
    """Bound a fresh XFS internal log plus allocation-group initialization.

    The dry run describes the exact mkfs geometry, including very large virtual
    LVs. Allow eight whole thin chunks per allocation group for headers/btrees,
    and an additional 64 MiB for initial inode/quota accounting. Unsupported
    output is an admission failure, never a zero-byte estimate.
    """
    groups = re.search(r'\bagcount=(\d+)', layout)
    log = re.search(r'^log\s*=\s*internal log\s+bsize=(\d+)\s+blocks=(\d+)', layout, re.M)
    data = re.search(r'^data\s*=\s*bsize=(\d+)\s+blocks=(\d+)', layout, re.M)
    if not groups or not log or not data or chunk_bytes <= 0:
        raise PressureError('cannot budget XFS initialization geometry')
    count, block, blocks = int(groups[1]), int(log[1]), int(log[2])
    if count <= 0 or block != QUANTUM or int(data[1]) != QUANTUM or blocks <= 0:
        raise PressureError('unsupported XFS initialization geometry')
    log_bytes = ((block * blocks + chunk_bytes - 1) // chunk_bytes) * chunk_bytes
    budget = log_bytes + count * 8 * chunk_bytes + 64 * 1024**2
    return ((budget + QUANTUM - 1) // QUANTUM) * QUANTUM


@contextmanager
def format_admission(device, storage_class):
    if not Registry().data.get('active'):
        yield
        return
    layout = command(['mkfs.xfs', '-N', device], timeout=30)
    budget = format_budget(layout, physical_sample().chunk_bytes)
    with reservation('volume-format', budget, storage_class=storage_class):
        yield


def mount_progress(**details):
    # This is progress reporting, not a readiness marker. Its failure must not
    # turn a successful mount into an apparent failure or trigger LV cleanup.
    try:
        atomic_json(RUN_DIR + '/mount.json', details)
    except OSError:
        pass
