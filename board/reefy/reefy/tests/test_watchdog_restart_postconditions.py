"""A timed-out restart needs positive evidence from the same container."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy import watchdog


CHECK = {'service': 'reefy-proxy', 'url': 'http://127.0.0.1:8080/',
         'kind': 'http-response'}
IDENTITY = 'a' * 64


class RestartPostconditions(unittest.TestCase):
    def exercise(self, before, after, healthy):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'reefy-proxy-failures').write_text('2')
            sentinel = root / 'unrelated-data'
            sentinel.write_bytes(b'preserved')
            operations = []
            states = iter((before, after))

            def run(args, **kwargs):
                operations.append(args)
                if args[1] == 'restart':
                    raise subprocess.TimeoutExpired(args, 30)
                self.assertEqual(args[1], 'inspect')
                value = next(states)
                if isinstance(value, BaseException):
                    raise value
                return type('Result', (), {'returncode': 0,
                    'stdout': json.dumps(value) if value is not None else 'invalid'})()

            output = io.StringIO()
            with patch.object(watchdog, 'find_container', return_value=IDENTITY), \
                    patch.object(watchdog, 'probe', side_effect=[False, healthy]), \
                    patch.object(watchdog.subprocess, 'run', side_effect=run), \
                    contextlib.redirect_stdout(output):
                watchdog.check_service(CHECK, directory, now=100)
            self.assertEqual(sentinel.read_bytes(), b'preserved')
            self.assertEqual(sum(a[1] == 'restart' for a in operations), 1)
            self.assertEqual((root / 'reefy-proxy-cooldown').read_text(), '400')
            return output.getvalue(), (root / 'reefy-proxy-failures').exists()

    def test_completed_restart_with_late_ack_is_accepted_without_replay(self):
        before = dict(id=IDENTITY, started='2020-01-01T00:00:00Z',
                      running=True, restarting=False)
        after = dict(before, started='2020-01-01T00:00:10Z')
        output, failed = self.exercise(before, after, True)
        self.assertIn('restart completed', output)
        self.assertNotIn('restart failed', output)
        self.assertFalse(failed)

    def test_unknown_unchanged_wrong_or_unhealthy_state_fails_closed(self):
        before = dict(id=IDENTITY, started='2020-01-01T00:00:00Z',
                      running=True, restarting=False)
        after = dict(before, started='2020-01-01T00:00:10Z')
        for old, new, healthy in (
                (before, before, True), (before, after, False),
                (before, dict(after, id='b' * 64), True),
                (before, dict(after, running=False), True),
                (before, dict(after, restarting=True), True),
                (before, dict(after, started='invalid'), True),
                (before, None, True), (None, after, True),
                (before, OSError('synthetic inspect failure'), True)):
            with self.subTest(old=old, new=new, healthy=healthy):
                output, failed = self.exercise(old, new, healthy)
                self.assertIn('restart failed or timed out', output)
                self.assertNotIn('restart completed', output)
                self.assertTrue(failed)
