from contextlib import nullcontext
import json
import os
import stat
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import _bootstrap  # noqa: F401
from reefy import storage_snapshot_admission as snapshot
from reefy.storage_pressure import GB, PoolSample, PressureError
from reefy.storage_guard import Guard
from reefy.storage_quota import Registry, atomic_json


class ReservationGuardTests(unittest.TestCase):
    def test_rejected_proposal_does_not_freeze_apps_or_spend_reserved_credit(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Registry(str(Path(directory, 'registry.json')))
            registry.data.update(active=True, inventory_complete=True, projects={}, leases={
                'too-large': {'bytes': 64 * GB, 'storage_class': 'state', 'target': None,
                              'admitted': False, 'burst_remaining': 64 * GB},
                'fits': {'bytes': GB, 'storage_class': 'state', 'target': None,
                         'admitted': False, 'burst_remaining': GB}})
            registry.save()
            with patch('reefy.storage_guard.state_lock', return_value=nullcontext()):
                result = Guard(peak_bytes_per_second=128 * 1024**2, response_seconds=40,
                    in_flight_bytes=64 * 1024**2, registry_path=registry.path,
                    status_path=str(Path(directory, 'status.json')),
                    sample=lambda: PoolSample(32 * GB, GB, 1, 1000, 524288)).pass_once()
            self.assertFalse(result['allocation']['quiesce'])
            self.assertEqual(result['admitted_leases'], ['fits'])
            leases = Registry(registry.path).data['leases']
            self.assertFalse(leases['too-large']['admitted'])
            self.assertTrue(leases['fits']['admitted'])
            self.assertEqual(leases['too-large']['burst_remaining'], 64 * GB)

    def test_budgeted_burst_is_spent_once_and_full_lease_stays_charged(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Registry(str(Path(directory, 'registry.json')))
            registry.data.update(active=True, inventory_complete=True, projects={}, leases={
                'snapshot': {'bytes': GB, 'storage_class': 'state', 'target': None,
                             'admitted': True, 'burst_remaining': GB}})
            registry.save()
            now, used = 0, GB
            def make_guard():
                return Guard(peak_bytes_per_second=128 * 1024**2, response_seconds=40,
                    in_flight_bytes=0, registry_path=registry.path, clock=lambda: now,
                    status_path=str(Path(directory, 'status.json')),
                    sample=lambda: PoolSample(128 * GB, used, 1, 1000, 524288))
            with patch('reefy.storage_guard.state_lock', return_value=nullcontext()):
                guard = make_guard()
                guard.pass_once()
                for _ in range(2):
                    now += .05
                    used += GB // 2
                    guard.pass_once()
                    self.assertEqual(guard.peak, 128 * 1024**2)
                lease = Registry(registry.path).data['leases']['snapshot']
                self.assertEqual(lease['bytes'], GB)
                self.assertEqual(lease['burst_remaining'], 0)
                guard = make_guard()
                guard.pass_once()
                now += .05
                used += GB // 2
                with self.assertRaises(PressureError):
                    guard.pass_once()
                self.assertGreater(guard.peak, 128 * 1024**2)


class SourceCounterTests(unittest.TestCase):
    def test_source_inventory_matches_kernel_device_despite_distinct_alias_nodes(self):
        path = '/synthetic/config'
        row = {'path': path, 'mount': path, 'filesystem': 'synthetic-xfs',
               'device': '253:8', 'complete': True, 'storage_class': 'state'}
        mount = {'target': path, 'source': '/dev/dm-8', 'uuid': 'synthetic-xfs',
                 'maj:min': '253:8'}
        with patch.object(snapshot, 'Registry', return_value=SimpleNamespace(data={'projects': {'p': row}})), \
                patch.object(snapshot, 'mount_info', return_value=mount), \
                patch.object(snapshot.os, 'stat', return_value=SimpleNamespace(
                    st_rdev=os.makedev(253, 8), st_mode=stat.S_IFBLK)), \
                patch.object(snapshot, 'mapped_bytes', return_value=1024**2):
            self.assertEqual(snapshot.source_inventory([path])[0]['bytes'], 1024**2)
            for field, wrong in (('maj:min', '253:9'), ('source', '/dev/dm-8[/subdir]'),
                                 ('uuid', 'wrong-filesystem'), ('target', '/another')):
                with self.subTest(field=field):
                    original = mount[field]
                    mount[field] = wrong
                    with self.assertRaises(PressureError):
                        snapshot.source_inventory([path])
                    mount[field] = original

    def test_sector_count_is_exact_despite_huge_virtual_size(self):
        with patch.object(snapshot, 'command', side_effect=[
                '0 200000000000 thin 253:2 7', '0 200000000000 thin 2048 199999999999']), \
                patch.object(snapshot.os, 'stat', return_value=SimpleNamespace(st_rdev=os.makedev(253, 2))):
            self.assertEqual(snapshot.mapped_bytes('/dev/synthetic'), 1024**2)

    def test_unknown_external_or_wrong_pool_mapping_is_rejected(self):
        for table, status in [
            ('0 100 thin 253:3 7', '0 100 thin 20 99'),
            ('0 100 thin 253:2 7 253:8', '0 100 thin 20 99'),
            ('0 100 thin 253:2 7', '0 100 thin 101 99'),
            ('0 100 thin 253:2 7', '0 100 thin Fail'),
        ]:
            with self.subTest(table=table, status=status), \
                    patch.object(snapshot, 'command', side_effect=[table, status]), \
                    patch.object(snapshot.os, 'stat', return_value=SimpleNamespace(st_rdev=os.makedev(253, 2))):
                with self.assertRaises(PressureError):
                    snapshot.mapped_bytes('/dev/synthetic')

    def test_mixed_sources_use_lower_priority_and_allocated_bytes(self):
        sources = [{'bytes': GB, 'storage_class': 'state'}, {'bytes': 2 * GB, 'storage_class': 'bulk'}]
        with patch.object(snapshot, 'source_inventory', return_value=sources):
            budget, kind = snapshot.reservation_size(['/synthetic/state', '/synthetic/bulk'])
        self.assertEqual(kind, 'bulk')
        self.assertEqual(budget, ((3 * GB + 3 * snapshot.SETUP_BYTES + 4095) // 4096) * 4096)


class CoordinatorTests(unittest.TestCase):
    def test_growth_race_rejects_before_snapshot_and_resumes_writers(self):
        self.run_creation(source_bytes=3 * GB, expected_success=False)

    def test_all_snapshots_created_after_draining_and_before_recovery(self):
        self.run_creation(source_bytes=GB, expected_success=True)

    def run_creation(self, *, source_bytes, expected_success):
        with tempfile.TemporaryDirectory() as directory:
            request_path = str(Path(directory, 'request.json'))
            request = {'paths': ['/synthetic/config'], 'instance_uuid': 'synthetic',
                       'timestamp': 12345, 'lease': 'reserved', 'finished': False}
            atomic_json(request_path, request)
            order = []
            def hold(*args):
                order.append('hold')
                atomic_json(str(Path(directory, 'hold.json')), {'monotonic': 1})
            def drained():
                order.append('drain')
                atomic_json(str(Path(directory, 'hold.json')), {'monotonic': 1, 'drained': True})
            create = Mock(side_effect=lambda *a: (order.append('create') or ('snap', '/synthetic/snap')))
            lease = {'kind': 'backup-snapshots', 'bytes': 2 * GB, 'burst_remaining': 2 * GB, 'admitted': True}
            with patch.object(snapshot, 'REQUEST', request_path), patch.object(snapshot, 'RUN_DIR', directory), \
                    patch.object(snapshot, 'Registry', return_value=SimpleNamespace(data={'leases': {'reserved': lease}})), \
                    patch.object(snapshot, 'source_inventory', return_value=[{'path': '/synthetic/config', 'bytes': source_bytes}]), \
                    patch.object(snapshot.runpy, 'run_path', return_value={'snapshot_volume': create}), \
                    patch('reefy.storage_watchdog.hold_writers', side_effect=hold), \
                    patch('reefy.storage_watchdog.hold_lock', return_value=nullcontext()), \
                    patch('reefy.storage_watchdog.finish_hold', side_effect=drained), \
                    patch('reefy.storage_watchdog.Writers') as writers, \
                    patch('reefy.storage_snapshots.cleanup_orphans') as cleanup, \
                    patch('reefy.storage_service.recover', side_effect=lambda **_: order.append('recover')):
                writers.return_value.frozen.return_value = True
                snapshot.coordinate()
            result = json.loads(Path(request_path).read_text())
            self.assertTrue(result['finished'])
            self.assertEqual(order[:2], ['hold', 'drain'])
            self.assertEqual(order[-1], 'recover')
            self.assertEqual(create.call_count, int(expected_success))
            self.assertEqual('error' not in result, expected_success)
            if not expected_success:
                cleanup.assert_called_once()

    def test_pending_request_prevents_lease_release_even_before_lv_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory, 'request.json'))
            with patch.object(snapshot, 'REQUEST', path):
                self.assertFalse(snapshot.pending_preparation())
                atomic_json(path, {'finished': False})
                with patch('reefy.storage_snapshots.backup_snapshots', return_value=[]):
                    from reefy.storage_snapshots import snapshots_released
                    self.assertFalse(snapshots_released())
                atomic_json(path, {'finished': True})
                self.assertFalse(snapshot.pending_preparation())

    def test_recovery_stops_producer_before_clearing_pending_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory, 'request.json'))
            atomic_json(path, {'finished': False})
            calls = []
            def command(args, **kwargs):
                calls.append(args)
                self.assertTrue(snapshot.pending_preparation())
                return 'active' if 'show' in args else ''
            with patch.object(snapshot, 'REQUEST', path), patch.object(snapshot, 'command', side_effect=command):
                self.assertTrue(snapshot.abort_preparation())
                self.assertFalse(snapshot.pending_preparation())
            self.assertEqual(calls[-1], ['systemctl', 'stop', snapshot.UNIT])
