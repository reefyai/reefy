import unittest
import _bootstrap  # noqa: F401
from reefy.storage_format import format_budget
from reefy.storage_pressure import PressureError


class FormatBudgetTests(unittest.TestCase):
    def test_internal_log_and_ag_headers_are_physical_reservations(self):
        layout = '''meta-data=/dev/synthetic isize=512 agcount=4, agsize=100000 blks
 data     = bsize=4096 blocks=400000, imaxpct=25
log      = internal log bsize=4096 blocks=16384, version=2
'''.replace('\n data', '\ndata')
        self.assertEqual(format_budget(layout, 512 * 1024),
                         (64 + 16 + 64) * 1024**2)
        large = layout.replace('agcount=4', 'agcount=64').replace('blocks=16384', 'blocks=524288')
        self.assertEqual(format_budget(large, 512 * 1024), (2048 + 256 + 64) * 1024**2)

    def test_unknown_or_external_geometry_cannot_be_underbudgeted(self):
        for layout in ('', 'meta-data=foo agcount=4\nlog = external log', 'malformed'):
            with self.assertRaises(PressureError):
                format_budget(layout, 512 * 1024)
