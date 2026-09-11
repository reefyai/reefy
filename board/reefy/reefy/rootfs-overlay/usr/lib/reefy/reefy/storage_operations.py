"""Admission at managed Compose boundaries, outside the fast monitor lock."""
from contextlib import contextmanager, ExitStack
import json
import threading

from reefy.storage_admission import reservation
from reefy.storage_cache import preserve_new_mounts
from reefy.storage_images import protect_images
from reefy.storage_pressure import PressureError
from reefy.storage_quota import Registry, RUN_DIR, state_lock
from reefy.storage_runtime import LAYER_INITIAL_SIZE


_local = threading.local()


@contextmanager
def compose_operation(compose, project_name, args):
    registry = Registry()
    if (getattr(_local, 'inside', False) or not registry.data.get('active')
            or not args or args[0] not in ('up', 'create', 'run', 'pull', 'start', 'restart')):
        yield
        return
    if isinstance(compose, str):
        with open(compose) as stream:
            compose = json.load(stream)
    services = compose.get('services') or {}
    # Dependencies can be created as well. Reserve all declared services rather
    # than undercounting a Compose dependency closure or init-layer creation.
    images = {service['image'] for service in services.values() if service.get('image')}
    with state_lock(RUN_DIR + '/operations.lock', timeout=30), ExitStack() as stack:
        _local.inside = True
        try:
            stack.enter_context(protect_images(images))
            if args[0] == 'pull' or (args[0] == 'up' and 'never' not in args):
                # This is conservative unpack runway, not a claim about image
                # compression ratio. All actual writes remain under project
                # limits; larger pulls may fail and be retried when space exists.
                targets = [key for key, record in registry.data['projects'].items()
                           if record['path'] in ('/mnt/reefy-data', '/mnt/reefy-data/docker/overlay2')
                           and not record.get('retired')]
                if len(targets) != 2:
                    raise PressureError('Docker pull destinations are not fully governed')
                for target in targets:
                    stack.enter_context(reservation('image-pull', 1024**3,
                                                    target=target))
            if args[0] in ('up', 'create', 'run'):
                stack.enter_context(reservation('container-create',
                                                max(1, len(services)) * 2 * LAYER_INITIAL_SIZE))
                preserve_new_mounts(compose, project_name)
            yield
        finally:
            # ExitStack releases leases while the operation remains serialized.
            stack.close()
            _local.inside = False
