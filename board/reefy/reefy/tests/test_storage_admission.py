from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy.storage_admission import reservation
from reefy.storage_pressure import (GB, Consumer, PoolSample, PressureError,
                                    allocate, admit_reservations)
from reefy.storage_quota import Registry


class AdmissionTests(unittest.TestCase):
    def test_targeted_and_cow_reservations_share_one_budget(self):
        sample = PoolSample(32 * GB, 4 * GB, 100, 1000, 524288)
        consumers = [Consumer('image', 'runtime', GB, 2 * GB),
                     Consumer('media', 'bulk', GB, 2 * GB)]
        leases = {'pull': {'bytes': 1024**3, 'target': 'image', 'storage_class': 'runtime'},
                  'snapshot': {'bytes': 1024**3, 'target': None, 'storage_class': 'runtime'}}
        planned = allocate(sample, consumers, pending_bytes=2 * 1024**3)
        applied, admitted = admit_reservations(planned, sample, consumers, leases, margin=0)
        self.assertEqual(set(admitted), set(leases))
        self.assertEqual(applied.limits['image'] - planned.limits['image'], 1024**3)
        self.assertEqual(applied.limits['media'], planned.limits['media'])
        self.assertLessEqual(sample.used + applied.granted + 1024**3, applied.boundaries.bulk)

    def test_reservation_cannot_override_native_layer_limit_or_wrong_class(self):
        sample = PoolSample(32 * GB, GB, 100, 1000, 524288)
        consumers = [Consumer('layer', 'runtime', 0, 1024, max_hard=16 * 1024**2)]
        lease = {'create': {'bytes': 16 * 1024**2, 'target': 'layer', 'storage_class': 'runtime'}}
        planned = allocate(sample, consumers, pending_bytes=16 * 1024**2)
        result, admitted = admit_reservations(planned, sample, consumers, lease, margin=0)
        self.assertEqual(admitted, [])
        self.assertEqual(result.limits, planned.limits)
        lease['create']['storage_class'] = 'state'
        with self.assertRaises(PressureError):
            admit_reservations(planned, sample, consumers, lease, margin=0)

    def test_failed_admission_and_operation_both_release_durable_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'registry.json')
            registry = Registry(path)
            registry.data.update(active=True, inventory_complete=True)
            registry.save()
            with patch('reefy.storage_admission.Registry', lambda: Registry(path)), \
                    patch('reefy.storage_admission.state_lock', return_value=nullcontext()), \
                    patch('reefy.storage_admission.wait_generation') as wait:
                with reservation('copy', 1024):
                    self.assertEqual(len(Registry(path).data['leases']), 1)
                self.assertEqual(Registry(path).data['leases'], {})
                wait.side_effect = PressureError('not enough space')
                with self.assertRaises(PressureError), reservation('copy', 1024):
                    self.fail('operation ran before admission')
                self.assertEqual(Registry(path).data['leases'], {})
                wait.side_effect = None
                with self.assertRaises(ValueError), reservation('copy', 1024):
                    raise ValueError('operation failed')
                self.assertEqual(Registry(path).data['leases'], {})


if __name__ == '__main__':
    unittest.main()
