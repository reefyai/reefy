"""Allocator invariants and exact physical-counter parsing."""
import random
import unittest

import _bootstrap  # noqa: F401
from reefy.storage_pressure import (
    GB, MIB, QUANTUM, Consumer, PoolSample, PressureError, allocate, apply_allocation,
    boundaries, parse_thin_sample,
)


def sample(used, capacity=512 * GB):
    return PoolSample(capacity, used, 100, 1000, 512 * 1024)


class BudgetTests(unittest.TestCase):
    def test_reviewed_disk_sizes(self):
        for size, bulk, runtime, state in [
                (32, 21.6, 24.8, 28), (128, 96, 108.8, 121.6),
                (512, 384, 435.2, 486.4), (1000, 750, 850, 950),
                (10000, 9750, 9850, 9950)]:
            with self.subTest(size=size):
                b = boundaries(size * GB)
                self.assertEqual((b.bulk, b.runtime, b.state),
                                 tuple(round(v * GB) for v in (bulk, runtime, state)))

    def test_rate_can_raise_reserve_above_absolute_base_cap(self):
        b = boundaries(10000 * GB, peak_bytes_per_second=3 * GB)
        self.assertEqual(b.emergency, 90 * GB)
        with self.assertRaises(PressureError):
            boundaries(3 * GB)

    def test_mixed_writers_never_duplicate_remaining_capacity(self):
        rng = random.Random(27)
        for _ in range(400):
            pool = sample(rng.randrange(480 * GB))
            writers = [Consumer(str(i), rng.choice(('bulk', 'runtime', 'state')),
                                rng.randrange(GB), 2 * GB, rng.randrange(1000000))
                       for i in range(rng.randrange(1, 30))]
            pending = rng.randrange(GB)
            plan = allocate(pool, writers, pending_bytes=pending)
            self.assertTrue(all(v > 0 for v in plan.limits.values()))
            if plan.quiesce:
                continue
            ceiling = getattr(plan.boundaries, plan.stage)
            outstanding = sum(max(0, plan.limits[c.key] - c.used) for c in writers)
            self.assertLessEqual(pool.used + pending + outstanding, ceiling)
            if plan.stage != 'bulk':
                for c in writers:
                    if c.storage_class == 'bulk':
                        self.assertLess(plan.limits[c.key] - c.used, QUANTUM)

    def test_large_existing_database_reduces_media_room(self):
        writers = [Consumer('media', 'bulk', 100 * GB, 200 * GB, 1000000),
                   Consumer('db', 'state', 200 * GB, 210 * GB, 1)]
        plan = allocate(sample(300 * GB), writers)
        self.assertLess(plan.granted, 84 * GB)
        self.assertLess(plan.limits['media'], 184 * GB)

    def test_metadata_and_readonly_pool_force_quiescing(self):
        for pool in [PoolSample(512 * GB, 20 * GB, 850, 1000, 524288),
                     PoolSample(512 * GB, 20 * GB, 100, 1000, 524288, False)]:
            plan = allocate(pool, [Consumer('new', 'bulk', 0, 0)])
            self.assertTrue(plan.quiesce)
            self.assertEqual(plan.limits['new'], QUANTUM)

    def test_deleting_files_without_physical_reclamation_does_not_refund(self):
        before = allocate(sample(385 * GB), [Consumer('media', 'bulk', 300 * GB, 310 * GB)])
        after = allocate(sample(385 * GB), [Consumer('media', 'bulk', 200 * GB, 310 * GB)])
        self.assertEqual(before.granted, after.granted)
        self.assertEqual(after.granted, 0)

    def test_sustained_writer_gets_runway_and_config_keeps_startup_room(self):
        writers = [Consumer('media', 'bulk', GB, 2 * GB, 5 * 1024**2),
                   Consumer('config', 'state', 100000, GB)]
        plan = allocate(sample(100 * GB), writers)
        self.assertGreater(plan.limits['media'] - GB, 5 * 1024**2 * 3600)
        self.assertLess(plan.limits['config'], 65 * 1024**2)

    def test_independent_hard_caps_are_respected(self):
        plan = allocate(sample(GB), [Consumer('a', 'runtime', GB, 2 * GB,
                                               max_hard=2 * GB)])
        self.assertEqual(plan.limits['a'], (2 * GB // QUANTUM) * QUANTUM)

    def test_partial_write_fragments_do_not_strand_higher_priority_bands(self):
        used = [4096] * 6
        hard = [4 * MIB] + [0] * 5
        classes = ['runtime', 'bulk', 'state', 'bulk', 'runtime', 'bulk']
        seen = set()
        for _ in range(100):
            physical = 128 * MIB + sum(((value + 524287) // 524288) * 524288 for value in used)
            writers = [Consumer(str(i), cls, used[i], hard[i], 0 if i == 0 else 8 * MIB,
                                4 * MIB if i == 0 else None) for i, cls in enumerate(classes)]
            plan = allocate(PoolSample(12 * 1024**3, physical, 100, 1000, 524288), writers,
                            peak_bytes_per_second=128 * MIB, response_seconds=4, in_flight_bytes=64 * MIB)
            seen.add(plan.stage)
            before = list(used)
            hard = [plan.limits[str(i)] for i in range(len(used))]
            for i in range(1, len(used)):
                used[i] += max(0, min(80 * MIB, ((hard[i] - used[i]) // MIB) * MIB))
            if used == before:
                break
        self.assertTrue({'bulk', 'runtime', 'state'} <= seen, seen)
        self.assertGreater(used[2], used[1])

    def test_unknown_limits_are_closed_before_grants(self):
        writers = [Consumer('new', 'state', 0, 0),
                   Consumer('old', 'bulk', 1024, 8 * GB)]
        plan = allocate(sample(100 * GB), writers)
        values = {c.key: c.hard for c in writers}
        actions = []
        def write(key, value):
            values[key] = value
            actions.append((key, value))
        apply_allocation(writers, plan, write, values.get)
        self.assertEqual(actions[0], ('new', QUANTUM))
        self.assertEqual(values, plan.limits)

    def test_failed_revoke_prevents_all_grants(self):
        writers = [Consumer('bulk', 'bulk', 1024, 20 * GB),
                   Consumer('db', 'state', 1024, 2048)]
        plan = allocate(sample(385 * GB), writers)
        actions = []
        with self.assertRaises(PressureError):
            apply_allocation(writers, plan, lambda *v: actions.append(v),
                             lambda key: 20 * GB)
        self.assertEqual(actions, [('bulk', QUANTUM)])


class CounterTests(unittest.TestCase):
    def test_exact_dm_counters_and_chunk_geometry(self):
        pool = parse_thin_sample(
            '0 104857600 thin-pool 8 123/1024 4567/100000 - rw discard_passdown queue_if_no_space -',
            '0 104857600 thin-pool 253:1 253:2 1024 0 1 skip_block_zeroing')
        self.assertEqual(pool.used, 4567 * 524288)
        self.assertEqual(pool.capacity, 100000 * 524288)
        self.assertEqual(pool.metadata_used, 123 * 4096)
        self.assertTrue(pool.healthy)

    def test_unknown_samples_are_not_zero_usage(self):
        for status in ('Fail', '', '0 100 thin-pool 1 - - - ro'):
            with self.assertRaises(PressureError):
                parse_thin_sample(status, 'bad table')


if __name__ == '__main__':
    unittest.main()
