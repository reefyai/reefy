import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401
from reefy.storage_pressure import GB, PoolSample, PressureError
from reefy.storage_watchdog import Writers, check, unhealthy_reason


class WatchdogTests(unittest.TestCase):
    def status(self):
        return {'sampled_monotonic': 90, 'completed_monotonic': 91,
                'allocation': {'boundaries': {'state': 28 * GB}, 'quiesce': False}}

    def test_stale_future_and_missing_heartbeats_cannot_prove_safety(self):
        sample = PoolSample(32 * GB, GB, 100, 1000, 524288)
        self.assertIsNone(unhealthy_reason(self.status(), sample, 100, stale_seconds=20))
        for status, now in ((self.status(), 111), (self.status(), 89), ({}, 100)):
            self.assertIsNotNone(unhealthy_reason(status, sample, now, stale_seconds=20))

    def test_fresh_heartbeat_does_not_override_physical_or_metadata_pressure(self):
        for sample in (PoolSample(32 * GB, 28 * GB, 100, 1000, 524288),
                       PoolSample(32 * GB, GB, 850, 1000, 524288),
                       PoolSample(32 * GB, GB, 100, 1000, 524288, False)):
            self.assertIsNotNone(unhealthy_reason(self.status(), sample, 100, stale_seconds=20))

    def test_transient_sampler_timeout_requires_fresh_success_before_continuing(self):
        from reefy.storage_watchdog import sample_with_retry
        healthy = PoolSample(32 * GB, GB, 100, 1000, 524288)
        with patch('reefy.storage_watchdog.physical_sample',
                   side_effect=[subprocess.TimeoutExpired('dmsetup', 2), healthy]) as sample:
            self.assertEqual(sample_with_retry(), healthy)
            self.assertEqual(sample.call_count, 2)
            self.assertTrue(all(call.kwargs == {'timeout': 2} for call in sample.call_args_list))
        with patch('reefy.storage_watchdog.physical_sample', side_effect=TimeoutError) as sample:
            with self.assertRaises(TimeoutError):
                sample_with_retry()
            self.assertEqual(sample.call_count, 2)
        with patch('reefy.storage_watchdog.physical_sample', side_effect=ValueError) as sample:
            with self.assertRaises(ValueError):
                sample_with_retry()
            self.assertEqual(sample.call_count, 1)

    def test_failed_sampler_freezes_writers_without_docker_api(self):
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / 'status.json'
            status.write_text(json.dumps(self.status()))
            writers = Mock()
            with patch('reefy.storage_watchdog.atomic_json') as save:
                reason = check(active=True, stale_seconds=20, writers=writers,
                               sample=Mock(side_effect=TimeoutError),
                               status_path=str(status), now=100)
            self.assertIn('unavailable', reason)
            save.assert_called_once()
            writers.freeze.assert_called_once()

    def test_cow_containment_starts_before_consuming_the_response_margin(self):
        status = dict(self.status(), physical_stop_bytes=24 * GB)
        self.assertIsNone(unhealthy_reason(status, PoolSample(32 * GB, 23 * GB, 100, 1000, 524288),
                                          100, stale_seconds=20))
        self.assertIsNotNone(unhealthy_reason(status, PoolSample(32 * GB, 24 * GB, 100, 1000, 524288),
                                             100, stale_seconds=20))
        status['physical_stop_bytes'] = 29 * GB
        self.assertIn('invalid', unhealthy_reason(status, PoolSample(32 * GB, GB, 100, 1000, 524288),
                                                 100, stale_seconds=20))

    def test_cgroup_freezer_preserves_control_and_checks_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('docker.slice', 'control'):
                (root / name).mkdir()
                (root / name / 'cgroup.freeze').write_text('0')
                (root / name / 'cgroup.events').write_text('populated 1\nfrozen 1\n')
            writers = Writers(directory, ('docker.slice',))
            writers.freeze()
            self.assertEqual((root / 'docker.slice/cgroup.freeze').read_text(), '1')
            self.assertEqual((root / 'control/cgroup.freeze').read_text(), '0')
            (root / 'docker.slice/cgroup.events').write_text('populated 1\nfrozen 0\n')
            with self.assertRaises(PressureError):
                writers.freeze(timeout=0)
            writers.thaw()
            self.assertEqual((root / 'docker.slice/cgroup.freeze').read_text(), '0')


if __name__ == '__main__':
    unittest.main()
