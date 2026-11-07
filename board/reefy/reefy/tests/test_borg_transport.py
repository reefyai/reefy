import io
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import _bootstrap
from reefy import borg_transport as transport

CLOSED = 'Connection closed by remote host. Is borg working on the server?'


class TransportTests(unittest.TestCase):
    def test_archive_lock_wait_fits_the_existing_remaining_budget(self):
        success = SimpleNamespace(returncode=0, stdout='synthetic/file\n', stderr='')
        with patch.object(transport.subprocess, 'run', return_value=success) as run, \
                patch.object(transport.time, 'monotonic', side_effect=[10, 10.2]):
            transport.list_archive('repo.invalid::synthetic', {}, lambda _: None, timeout_s=4)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--lock-wait') + 1], '3')
        self.assertAlmostEqual(run.call_args.kwargs['timeout'], 3.8)

    def test_extract_waits_for_locks_without_mutating_the_callers_command(self):
        command = ['borg', '--log-json', 'extract', '--progress', 'repo.invalid::synthetic']
        original = command.copy()
        success = SimpleNamespace(stderr=io.BytesIO(b''), returncode=0, wait=lambda: None)
        with patch.object(transport.subprocess, 'Popen', return_value=success) as start, \
                patch.object(transport.time, 'monotonic', side_effect=[0, .2]):
            self.assertEqual(transport.extract_archive(command, {}, '/synthetic', lambda _: None), 0)
        actual = start.call_args.args[0]
        self.assertEqual(actual[actual.index('--lock-wait') + 1], '120')
        self.assertEqual(command, original)

    def test_extract_never_starts_a_new_process_after_its_budget(self):
        with patch.object(transport.subprocess, 'Popen') as start, \
                patch.object(transport.time, 'monotonic', side_effect=[0, 2]):
            with self.assertRaises(subprocess.TimeoutExpired):
                transport.extract_archive(['borg', 'extract', 'synthetic'], {},
                                          '/synthetic', lambda _: None, timeout_s=1)
        start.assert_not_called()

    def test_auth_and_integrity_errors_remain_fatal(self):
        for error in ('Permission denied', 'Invalid passphrase', 'Data integrity error',
                      'Repository does not exist', 'Quota exceeded'):
            self.assertFalse(transport.retryable(CLOSED + '\n' + error))
        self.assertTrue(transport.retryable(CLOSED))
        self.assertFalse(transport.retryable('unexpected remote failure'))

    def test_archive_read_recovers_without_changing_assertion_data(self):
        failed = SimpleNamespace(returncode=2, stdout='', stderr=CLOSED)
        success = SimpleNamespace(returncode=0, stdout='synthetic/file\n', stderr='')
        with patch.object(transport.subprocess, 'run', side_effect=[failed, success]) as run, \
                patch.object(transport.time, 'sleep'):
            result = transport.list_archive('repo.invalid::synthetic', {}, lambda _: None)
        self.assertIs(result, success)
        self.assertEqual(run.call_count, 2)

    def test_archive_read_exhaustion_stays_failed(self):
        failed = SimpleNamespace(returncode=2, stdout='', stderr=CLOSED)
        with patch.object(transport.subprocess, 'run', return_value=failed) as run, \
                patch.object(transport.time, 'sleep'):
            self.assertIs(transport.list_archive('repo.invalid::synthetic', {}, lambda _: None), failed)
        self.assertEqual(run.call_count, 3)

    def test_extract_recovery_streams_real_finished_progress(self):
        first = SimpleNamespace(stderr=io.BytesIO(CLOSED.encode()), returncode=2, wait=lambda: None)
        second = SimpleNamespace(stderr=io.BytesIO(b'{"type":"progress_percent","finished":true}\n'), returncode=0, wait=lambda: None)
        messages = []
        with patch.object(transport.subprocess, 'Popen', side_effect=[first, second]) as start, \
                patch.object(transport.time, 'sleep'):
            result = transport.extract_archive(['borg', 'extract', 'synthetic'], {}, '/synthetic', messages.append)
        self.assertEqual(result, 0)
        self.assertEqual(start.call_count, 2)
        self.assertTrue(any('finished' in message for message in messages))

    def test_failed_extract_never_turns_into_success(self):
        def failed(*args, **kwargs):
            return SimpleNamespace(stderr=io.BytesIO(CLOSED.encode()), returncode=2, wait=lambda: None)
        with patch.object(transport.subprocess, 'Popen', side_effect=failed) as start, \
                patch.object(transport.time, 'sleep'):
            self.assertEqual(transport.extract_archive(['borg', 'extract'], {}, '/synthetic', lambda _: None), 2)
        self.assertEqual(start.call_count, 3)

    def test_extract_does_not_retry_corruption_or_auth_failure(self):
        for error in ('Data integrity error', 'Permission denied'):
            proc = SimpleNamespace(stderr=io.BytesIO((CLOSED+'\n'+error).encode()), returncode=2, wait=lambda: None)
            with patch.object(transport.subprocess, 'Popen', return_value=proc) as start:
                self.assertEqual(transport.extract_archive(['borg', 'extract'], {}, '/synthetic', lambda _: None), 2)
            self.assertEqual(start.call_count, 1)

    def test_extract_retry_does_not_reset_total_time_budget(self):
        proc = SimpleNamespace(stderr=io.BytesIO(CLOSED.encode()), returncode=2, wait=lambda: None)
        with patch.object(transport.subprocess, 'Popen', return_value=proc) as start, \
                patch.object(transport.time, 'monotonic', side_effect=[0, .1, .5, 1]), \
                patch.object(transport.time, 'sleep') as sleep:
            self.assertEqual(transport.extract_archive(['borg', 'extract'], {}, '/synthetic', lambda _: None, timeout_s=1), 2)
        self.assertEqual(start.call_count, 1)
        sleep.assert_not_called()
