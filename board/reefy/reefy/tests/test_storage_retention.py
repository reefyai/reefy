import unittest
from datetime import datetime, timezone

import _bootstrap  # noqa: F401
from reefy.storage_retention import observe, remove_eligible, three_months_later


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class RetentionTests(unittest.TestCase):
    def test_calendar_expiry_clamps_month_end_and_preserves_time(self):
        self.assertEqual(three_months_later(utc('2032-01-31T12:34:56')),
                         utc('2032-04-30T12:34:56'))
        self.assertEqual(three_months_later(utc('2031-11-30T00:00:00')),
                         utc('2032-02-29T00:00:00'))

    def test_new_history_does_not_use_image_build_age(self):
        state, candidates = observe(None, ['sha256:old'], [], utc('2032-01-01'),
                                    clock_trusted=True)
        self.assertEqual(candidates, [])
        state, candidates = observe(state, ['sha256:old'], [], utc('2032-04-01'),
                                    clock_trusted=True)
        self.assertEqual(candidates, ['sha256:old'])

    def test_referencing_resets_three_month_window(self):
        state, _ = observe(None, ['x'], [], utc('2032-01-01'), clock_trusted=True)
        state, _ = observe(state, ['x'], ['x'], utc('2032-03-31'), clock_trusted=True)
        state, candidates = observe(state, ['x'], [], utc('2032-04-01'), clock_trusted=True)
        self.assertEqual(candidates, [])
        self.assertEqual(state['images']['x']['unreferenced_since'], utc('2032-04-01'))

    def test_unreliable_clock_cannot_age_out_images(self):
        state, _ = observe(None, ['x'], [], utc('2032-01-01'), clock_trusted=True)
        state, candidates = observe(state, ['x'], [], utc('2032-05-01'), clock_trusted=False)
        self.assertEqual(candidates, [])
        state, candidates = observe(state, ['x'], [], utc('2032-05-02'), clock_trusted=True)
        self.assertEqual(candidates, [])
        state, candidates = observe(state, ['x'], [], utc('2032-04-30'), clock_trusted=True)
        self.assertEqual(candidates, [])

    def test_new_reference_wins_at_removal_boundary(self):
        removed = []
        self.assertEqual(remove_eligible(['a', 'b'], lambda: {'b'}, removed.append), ['a'])
        self.assertEqual(removed, ['a'])


if __name__ == '__main__':
    unittest.main()
