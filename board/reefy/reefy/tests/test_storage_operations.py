from contextlib import contextmanager, nullcontext
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy import storage_operations as operations


class OperationCleanupTests(unittest.TestCase):
    def test_failed_cleanup_cannot_bypass_admission_for_the_next_operation(self):
        entered = []
        @contextmanager
        def protection(images):
            entered.append(images)
            yield
            raise subprocess.TimeoutExpired('synthetic-docker-inventory', 10)

        with patch.object(operations, 'Registry', return_value=SimpleNamespace(data={'active': True})), \
                patch.object(operations, 'state_lock', return_value=nullcontext()), \
                patch.object(operations, 'protect_images', protection):
            for _ in range(2):
                with self.assertRaises(subprocess.TimeoutExpired):
                    with operations.compose_operation({'services': {'app': {'image': 'synthetic:1'}}},
                                                      'synthetic', ['restart']):
                        self.assertTrue(operations._local.inside)
                self.assertFalse(operations._local.inside)
        self.assertEqual(entered, [{'synthetic:1'}, {'synthetic:1'}])
