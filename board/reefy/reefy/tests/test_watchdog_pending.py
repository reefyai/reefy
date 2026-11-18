"""Late Docker completion is observed across timer ticks, never replayed."""
import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from reefy import watchdog

CHECK = {'service': 'reefy-proxy', 'url': 'http://127.0.0.1:8080/', 'kind': 'http-response'}
IDENTITY = 'a' * 64


class PendingRestartTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.pending = self.root / 'reefy-proxy-pending.json'
        self.failures = self.root / 'reefy-proxy-failures'
        self.failures.write_text('2')
        self.marker = self.root / 'unrelated-data'
        self.marker.write_bytes(b'synthetic identity and application data')
        self.clock = 0.0
        self.late = False
        self.healthy = False
        self.commands = []
        self.output = io.StringIO()
        self.before = dict(id=IDENTITY, started=datetime(2020, 1, 1, tzinfo=timezone.utc),
                           running=True, restarting=False)
        self.after = dict(self.before, started=datetime(2020, 1, 2, tzinfo=timezone.utc))
        for context in (
            patch.object(watchdog, 'find_container', return_value=IDENTITY),
            patch.object(watchdog, '_container_state', side_effect=lambda *a: self.after if self.late else self.before),
            patch.object(watchdog, 'probe', side_effect=lambda *a, **k: self.healthy),
            patch.object(watchdog.subprocess, 'run', side_effect=self.command),
            patch.object(watchdog, 'time', SimpleNamespace(
                monotonic=lambda: self.clock, sleep=self.sleep, time=lambda: 100)),
            contextlib.redirect_stdout(self.output),
        ):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def sleep(self, seconds): self.clock += seconds

    def command(self, args, **kwargs):
        self.commands.append(args)
        self.assertEqual(args, ['docker', 'restart', '--time', '5', IDENTITY])
        self.clock += kwargs['timeout']
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])

    def tick(self, now):
        return watchdog.check_service(CHECK, str(self.root), now=now)

    def start_pending(self):
        self.assertFalse(self.tick(100))
        self.assertTrue(self.pending.exists(), self.output.getvalue())
        self.assertNotIn('restart failed', self.output.getvalue())
        self.assertNotIn('restart completed', self.output.getvalue())
        self.assertEqual(self.failures.read_text(), '3')
        self.assertEqual((self.root / 'reefy-proxy-cooldown').read_text(), '400')

    def assert_preserved(self):
        self.assertEqual(self.marker.read_bytes(), b'synthetic identity and application data')

    def test_late_completion_is_proven_on_next_tick_without_second_restart(self):
        self.start_pending()
        self.late = self.healthy = True
        self.assertFalse(self.tick(180))
        self.assertFalse(self.pending.exists())
        self.assertFalse(self.failures.exists())
        self.assertIn('restart completed', self.output.getvalue())
        self.assertEqual(len(self.commands), 1)
        self.assert_preserved()

    def test_new_identity_old_start_unhealthy_or_unknown_cannot_prove_completion(self):
        self.start_pending()
        self.late = True
        for change in (dict(id='b'*64), dict(started=self.before['started']),
                       dict(running=False), dict(restarting=True), None):
            with self.subTest(change=change):
                self.healthy = True
                self.after = dict(self.before, started=datetime(2020, 1, 2, tzinfo=timezone.utc))
                self.after.update(change or {})
                if change is None: self.after = None
                self.assertFalse(self.tick(180))
                self.assertTrue(self.pending.exists())
                self.assertNotIn('restart completed', self.output.getvalue())
        self.after = dict(self.before, started=datetime(2020, 1, 2, tzinfo=timezone.utc))
        self.healthy = False
        self.assertFalse(self.tick(180))
        self.assertTrue(self.pending.exists())
        self.assertEqual(len(self.commands), 1)
        self.assert_preserved()

    def test_pending_expiry_reports_failure_without_replaying_in_that_tick(self):
        self.start_pending()
        self.assertFalse(self.tick(400))
        self.assertFalse(self.pending.exists())
        self.assertIn('restart failed or timed out', self.output.getvalue())
        self.assertEqual(len(self.commands), 1)
        self.assert_preserved()

    def test_health_arriving_after_pending_deadline_cannot_acknowledge_restart(self):
        self.start_pending()
        self.late = True
        def slow_health(*args, **kwargs):
            self.clock += 2
            return True
        with patch.object(watchdog, 'probe', side_effect=slow_health):
            self.assertFalse(self.tick(399))
        self.assertTrue(self.pending.exists())
        self.assertNotIn('restart completed', self.output.getvalue())
        self.assertEqual(len(self.commands), 1)
        self.assert_preserved()

    def test_malformed_receipt_cannot_authorize_a_new_mutation(self):
        for value in ('not-json', '{}', json.dumps(dict(id=IDENTITY, started='unknown', expires=400)),
                      json.dumps(dict(id='sibling', started=self.before['started'].isoformat(), expires=400)),
                      json.dumps(dict(id=IDENTITY, started=self.before['started'].isoformat(), expires=True))):
            with self.subTest(value=value):
                self.pending.write_text(value)
                self.assertFalse(self.tick(180))
                self.assertEqual(self.commands, [])
                self.assertEqual(self.pending.read_text(), value)
        self.assert_preserved()

    def test_intent_is_atomic_before_submission_and_explicit_result_clears_it(self):
        receipts = []
        for status in (0, 1):
            with self.subTest(status=status):
                self.failures.write_text('2')
                (self.root / 'reefy-proxy-cooldown').unlink(missing_ok=True)
                def explicit(args, **kwargs):
                    receipt = json.loads(self.pending.read_text())
                    self.assertEqual(receipt['id'], IDENTITY)
                    receipts.append(receipt)
                    return SimpleNamespace(returncode=status)
                with patch.object(watchdog.subprocess, 'run', side_effect=explicit):
                    self.assertFalse(self.tick(100))
                self.assertFalse(self.pending.exists())
        self.assertEqual(len(receipts), 2)
        self.assert_preserved()

    def test_interruption_after_submission_preserves_cooldown_without_replay(self):
        def interrupted(args, **kwargs):
            self.commands.append(args)
            self.assertTrue(self.pending.exists())
            self.assertEqual((self.root / 'reefy-proxy-cooldown').read_text(), '400')
            raise KeyboardInterrupt()
        with patch.object(watchdog.subprocess, 'run', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.tick(100)
        self.late = self.healthy = True
        self.tick(180)
        self.assertFalse(self.pending.exists())
        self.healthy = False
        self.failures.write_text('2')
        self.tick(200)
        self.assertEqual(len(self.commands), 1)
        self.assertIn('recovery suppressed by cooldown', self.output.getvalue())
        self.assert_preserved()
