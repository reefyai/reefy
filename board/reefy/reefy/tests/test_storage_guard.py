from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy.storage_guard import Guard
from reefy.storage_pressure import GB, PoolSample, QUANTUM
from reefy.storage_quota import Registry
from reefy.storage_runtime import LAYER_INITIAL_SIZE, LAYER_SIZE


class NativeAllowanceTests(unittest.TestCase):
    def test_removed_layer_dquot_is_closed_before_live_layers_receive_runway(self):
        base, live, removed = 2**20, 2**20 + 2, 2**20 + 3
        with tempfile.TemporaryDirectory() as directory:
            registry = Registry(str(Path(directory) / 'registry.json'))
            registry.data.update(active=True, inventory_complete=True,
                docker={'base_project': base, 'filesystem': 'synthetic'},
                projects={'home': {'path': '/test/overlay2', 'mount': '/test',
                    'filesystem': 'synthetic', 'device': '253:1', 'project': base,
                    'storage_class': 'runtime', 'complete': True}})
            registry.save()
            quotas = {base: {'used': QUANTUM, 'hard': GB, 'soft': 0},
                      live: {'used': 0, 'hard': LAYER_INITIAL_SIZE, 'soft': 0},
                      removed: {'used': 0, 'hard': LAYER_SIZE, 'soft': 0}}
            writes = []

            def set_limit(mount, project, hard):
                quotas[project]['hard'] = hard
                writes.append((project, hard))

            with patch('reefy.storage_guard.state_lock', return_value=nullcontext()), \
                    patch('reefy.storage_guard.mount_info', return_value={'uuid': 'synthetic', 'maj:min': '253:1', 'target': '/test'}), \
                    patch('reefy.storage_guard.require_enforcement'), \
                    patch('reefy.storage_guard.active_native_projects', return_value={live}), \
                    patch('reefy.storage_guard.read_quotas', return_value=quotas), \
                    patch('reefy.storage_guard.set_quota', side_effect=set_limit):
                Guard(peak_bytes_per_second=1, response_seconds=30, in_flight_bytes=0,
                      registry_path=registry.path, status_path=str(Path(directory) / 'status.json'),
                      sample=lambda: PoolSample(32 * GB, GB, 100, 1000, 524288)).pass_once()
            self.assertEqual(writes[0], (removed, QUANTUM))
            self.assertEqual(quotas[live]['hard'], LAYER_SIZE)
            self.assertEqual(quotas[removed]['hard'], QUANTUM)
