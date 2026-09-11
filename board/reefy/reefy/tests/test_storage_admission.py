from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy.storage_admission import (reservation, quiesced_reservation,
                                    release_previous_boot_leases,
                                    release_reclaimed_snapshot_leases)
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
                with reservation('copy', 4096):
                    self.assertEqual(len(Registry(path).data['leases']), 1)
                self.assertEqual(Registry(path).data['leases'], {})
                wait.side_effect = PressureError('not enough space')
                with self.assertRaises(PressureError), reservation('copy', 4096):
                    self.fail('operation ran before admission')
                self.assertEqual(Registry(path).data['leases'], {})
                wait.side_effect = None
                with self.assertRaises(ValueError), reservation('copy', 4096):
                    raise ValueError('operation failed')
                self.assertEqual(Registry(path).data['leases'], {})





class BootAdmissionTests(unittest.TestCase):
    def test_snapshot_lease_survives_owner_boot_until_persistent_resource_is_gone(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Registry(str(Path(directory) / 'registry.json'))
            registry.data['leases'] = {
                'snapshot': {'boot_id': 'previous-boot', 'kind': 'backup-snapshots'},
                'live': {'boot_id': 'current-boot', 'kind': 'download'},
            }
            registry.save()
            with patch('reefy.storage_admission.boot_identity', return_value='current-boot'), \
                    patch('reefy.storage_snapshots.snapshots_released', return_value=False):
                with self.assertRaises(PressureError):
                    release_previous_boot_leases(registry)
                with self.assertRaises(PressureError):
                    release_reclaimed_snapshot_leases(registry)
            self.assertEqual(len(Registry(registry.path).data['leases']), 2)
            with patch('reefy.storage_snapshots.snapshots_released', return_value=True):
                self.assertEqual(release_reclaimed_snapshot_leases(registry), 1)
            self.assertEqual(set(Registry(registry.path).data['leases']), {'live'})

    def test_failed_teardown_or_unknown_inventory_retains_reservation(self):
        for outcome in (False, PressureError('inventory unavailable')):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / 'registry.json')
                registry = Registry(path)
                registry.data.update(active=True, inventory_complete=True)
                registry.save()
                def released():
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                with patch('reefy.storage_admission.Registry', lambda: Registry(path)), \
                        patch('reefy.storage_admission.state_lock', return_value=nullcontext()), \
                        patch('reefy.storage_admission.wait_generation'):
                    with self.assertRaises(PressureError), reservation(
                            'backup-snapshots', 4096, release_check=released):
                        pass
                self.assertEqual(len(Registry(path).data['leases']), 1)

    def test_rejected_operation_needs_no_resource_teardown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'registry.json')
            registry = Registry(path)
            registry.data.update(active=True, inventory_complete=True)
            registry.save()
            with patch('reefy.storage_admission.Registry', lambda: Registry(path)), \
                    patch('reefy.storage_admission.state_lock', return_value=nullcontext()), \
                    patch('reefy.storage_admission.wait_generation', side_effect=PressureError('denied')), \
                    patch('reefy.storage_snapshots.snapshots_released') as released:
                with self.assertRaises(PressureError), reservation(
                        'backup-snapshots', 4096, release_check=released):
                    self.fail('rejected operation ran')
                released.assert_not_called()
            self.assertEqual(Registry(path).data['leases'], {})

    def test_only_proven_previous_boot_leases_are_reclaimed(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Registry(str(Path(directory) / 'registry.json'))
            registry.data['leases'] = {
                'old': {'boot_id': 'previous-boot', 'pid': 100},
                'live': {'boot_id': 'current-boot', 'pid': 100},
                'unknown': {'pid': 101},
            }
            registry.save()
            with patch('reefy.storage_admission.boot_identity', return_value='current-boot'):
                self.assertEqual(release_previous_boot_leases(registry), 1)
            self.assertEqual(set(Registry(registry.path).data['leases']), {'live', 'unknown'})
            with patch('reefy.storage_admission.boot_identity', return_value=None):
                with self.assertRaises(PressureError):
                    release_previous_boot_leases(registry)

    def test_boot_can_budget_with_guard_unavailable_but_writers_stopped(self):
        sample = PoolSample(32 * GB, GB, 100, 1000, 524288)
        with patch('reefy.storage_admission.command', return_value='0'), \
                patch('reefy.storage_quota.physical_sample', return_value=sample), \
                patch('reefy.storage_admission.wait_generation') as wait:
            with quiesced_reservation('volume-format', 128 * 1024**2):
                pass
            wait.assert_not_called()

    def test_boot_refuses_active_writers_or_insufficient_physical_headroom(self):
        with patch('reefy.storage_admission.command', return_value='55'):
            with self.assertRaises(PressureError), quiesced_reservation('format', 4096):
                self.fail('boot bypassed an active writer')
        with patch('reefy.storage_admission.command', return_value='0'), \
                patch('reefy.storage_quota.physical_sample', return_value=PoolSample(32 * GB, 28 * GB, 100, 1000, 524288)):
            with self.assertRaises(PressureError), quiesced_reservation('format', 4096):
                self.fail('boot initialization ignored real capacity')


if __name__ == '__main__':
    unittest.main()
