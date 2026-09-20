"""Unit tests for the reefy-backup runner.

reefy-backup is a /usr/bin script (no .py), so load it by path. Tests the
fs-type-aware snapshot mount plus bounded remote-repository readiness and
best-effort post-archive maintenance."""

import importlib.util
import json
import tempfile
from pathlib import Path
import os
import types
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), '..', 'rootfs-overlay',
                     'usr', 'bin', 'reefy-backup')
# reefy-backup has no .py extension, so use an explicit source loader
# (spec_from_file_location can't infer a loader and returns None).
_loader = SourceFileLoader('reefy_backup', _PATH)
_spec = importlib.util.spec_from_loader('reefy_backup', _loader)
reefy_backup = importlib.util.module_from_spec(_spec)
_loader.exec_module(reefy_backup)


class SnapshotFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.pending = Path(directory.name) / 'pending.json'
        patch = mock.patch.object(reefy_backup, 'PENDING_SNAPSHOT_FILE', str(self.pending))
        patch.start()
        self.addCleanup(patch.stop)


class SnapshotMountOptsTests(SnapshotFixture):
    def _mount_opts_for(self, fstype):
        """Run snapshot_volume with lvcreate/blkid/mount mocked; return the
        mount option string that would be used for a snapshot of `fstype`."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ['dmsetup', 'info']:
                return types.SimpleNamespace(returncode=0, stdout='L--w', stderr='')
            if cmd and cmd[0] == 'blkid':
                return types.SimpleNamespace(returncode=0, stdout=fstype + '\n', stderr='')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(reefy_backup.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(reefy_backup.os, 'makedirs'):
            reefy_backup.snapshot_volume(
                '/mnt/reefy-data/apps/i1/config', 'i1', 1234567890)

        mount_cmd = next(c for c in calls if c and c[0] == 'mount')
        # mount -o <opts> <dev> <mnt>
        return mount_cmd[mount_cmd.index('-o') + 1]

    def test_xfs_snapshot_uses_nouuid_norecovery(self):
        opts = self._mount_opts_for('xfs')
        self.assertIn('nouuid', opts)
        self.assertIn('norecovery', opts)
        self.assertTrue(opts.split(',')[0] == 'ro', f'expected read-only: {opts}')

    def test_ext4_snapshot_plain_ro(self):
        self.assertEqual(self._mount_opts_for('ext4'), 'ro')

    def test_volume_lv_name_matches_storage(self):
        # Must mirror reefy.storage.Storage._volume_lv_name so the snapshot
        # targets the right LV.
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..',
                                        'rootfs-overlay', 'usr', 'lib', 'reefy'))
        from reefy.storage import Storage
        path = '/mnt/reefy-data/apps/i1/config'
        self.assertEqual(reefy_backup.volume_lv_name(path),
                         Storage()._volume_lv_name(path))


class SnapshotFailureRecoveryTests(SnapshotFixture):
    def setUp(self):
        super().setUp()
        self.path = '/synthetic/app/config'
        self.origin = f'/dev/reefy/{reefy_backup.volume_lv_name(self.path)}'
        self.suspended = False
        self.suspend_on_create = True
        self.resume_fails = False
        self.resume_leaves_suspended = False
        self.create_error = reefy_backup.subprocess.TimeoutExpired(
            cmd=['lvcreate', '--snapshot'], timeout=300)
        self.commands = []

    def run_command(self, cmd, **kwargs):
        self.commands.append(cmd)
        if cmd[:2] == ['dmsetup', 'info']:
            return types.SimpleNamespace(
                returncode=0, stdout='L-sw' if self.suspended else 'L--w',
                stderr='')
        if cmd[:2] == ['dmsetup', 'resume']:
            self.assertEqual(cmd[-1], self.origin)
            if not self.resume_fails and not self.resume_leaves_suspended:
                self.suspended = False
            return types.SimpleNamespace(
                returncode=1 if self.resume_fails else 0,
                stdout='', stderr='synthetic resume failure')
        if cmd[0] == 'lvcreate':
            self.assertEqual(kwargs['timeout'], 300)
            self.assertEqual(json.loads(self.pending.read_text())['lv_path'], self.origin)
            self.suspended = self.suspend_on_create
            if self.create_error:
                raise self.create_error
            return types.SimpleNamespace(
                returncode=5, stdout='', stderr='synthetic lvcreate failure')
        self.fail(f'unexpected command: {cmd}')

    def snapshot(self):
        return reefy_backup.snapshot_volume(self.path, 'synthetic', 1234567890)

    def release(self, *args):
        self.assertFalse(self.suspended, 'resume origin before orphan cleanup')
        return True

    def test_timeout_resumes_origin_before_cleanup_and_preserves_error(self):
        with mock.patch.object(reefy_backup.subprocess, 'run',
                               side_effect=self.run_command), \
                mock.patch.object(reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(reefy_backup, 'release_snapshot',
                                  side_effect=self.release) as release:
            with self.assertRaisesRegex(RuntimeError, 'timed out after 300s; app volume active') as error:
                self.snapshot()
        self.assertIs(error.exception.__cause__, self.create_error)
        self.assertFalse(self.pending.exists())
        self.assertFalse(self.suspended)
        release.assert_called_once()

    def test_nonzero_exit_also_resumes_origin(self):
        self.create_error = None
        with mock.patch.object(reefy_backup.subprocess, 'run',
                               side_effect=self.run_command), \
                mock.patch.object(reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(reefy_backup, 'release_snapshot',
                                  side_effect=self.release):
            with self.assertRaisesRegex(RuntimeError, 'synthetic lvcreate failure'):
                self.snapshot()
        self.assertFalse(self.suspended)

    def test_active_origin_is_not_resumed_after_failure(self):
        self.suspend_on_create = False
        with mock.patch.object(reefy_backup.subprocess, 'run',
                               side_effect=self.run_command), \
                mock.patch.object(reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(reefy_backup, 'release_snapshot', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'timed out after 300s; app volume active'):
                self.snapshot()
        self.assertFalse(any(cmd[:2] == ['dmsetup', 'resume']
                             for cmd in self.commands))

    def test_preexisting_suspension_is_not_owned_by_this_snapshot(self):
        self.suspended = True
        with mock.patch.object(reefy_backup.subprocess, 'run',
                               side_effect=self.run_command), \
                mock.patch.object(reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(reefy_backup, 'release_snapshot') as release:
            with self.assertRaisesRegex(RuntimeError, 'already suspended'):
                self.snapshot()
        self.assertTrue(self.suspended)
        self.assertFalse(any(cmd[0] == 'lvcreate' for cmd in self.commands))
        self.assertFalse(any(cmd[:2] == ['dmsetup', 'resume']
                             for cmd in self.commands))
        release.assert_not_called()

    def test_resume_failure_surfaces_without_attempting_lvremove(self):
        self.resume_fails = True
        with mock.patch.object(reefy_backup.subprocess, 'run',
                               side_effect=self.run_command), \
                mock.patch.object(reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(reefy_backup, 'release_snapshot') as release:
            with self.assertRaisesRegex(RuntimeError, 'origin recovery failed'):
                self.snapshot()
        self.assertTrue(self.suspended)
        release.assert_not_called()
        self.assertTrue(self.pending.exists())

    def test_resume_exit_zero_still_requires_active_origin(self):
        self.resume_leaves_suspended = True
        with mock.patch.object(reefy_backup.subprocess, 'run',
                               side_effect=self.run_command), \
                mock.patch.object(reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(reefy_backup, 'release_snapshot') as release:
            with self.assertRaisesRegex(RuntimeError, 'origin recovery failed'):
                self.snapshot()
        self.assertTrue(self.suspended)
        release.assert_not_called()

    def test_cleanup_failure_does_not_hide_original_snapshot_error(self):
        with mock.patch.object(reefy_backup.subprocess, 'run',
                               side_effect=self.run_command), \
                mock.patch.object(reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(reefy_backup, 'release_snapshot',
                                  side_effect=OSError('synthetic cleanup failure')):
            with self.assertRaisesRegex(RuntimeError, 'timed out after 300s; app volume active') as error:
                self.snapshot()
        self.assertIs(error.exception.__cause__, self.create_error)
        self.assertFalse(self.pending.exists())
        self.assertFalse(self.suspended)


class CrashCleanupTests(SnapshotFixture):
    def write_pending(self):
        self.pending.write_text(json.dumps({
            'instance_uuid': 'synthetic-instance',
            'lv_path': '/dev/reefy/reefy_backup_synthetic',
            'snap_name': 'reefy_snap_synthetic',
            'snap_mnt': '/synthetic/snapshot/config',
        }))

    def test_cleanup_restores_before_reporting_and_is_idempotent(self):
        self.write_pending()
        events = []
        with mock.patch.object(reefy_backup, '_snapshot_origin_suspended',
                               side_effect=[True, False]), \
                mock.patch.object(reefy_backup.subprocess, 'run',
                    side_effect=lambda *a, **k: (
                        events.append('resume') or types.SimpleNamespace(returncode=0))), \
                mock.patch.object(reefy_backup, 'release_snapshot', return_value=True), \
                mock.patch.object(reefy_backup, 'publish_status',
                    side_effect=lambda payload: events.append(payload)):
            reefy_backup.recover_pending_snapshot()
            reefy_backup.recover_pending_snapshot()
        self.assertEqual(events[0], 'resume')
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]['status'], 'error')
        self.assertIn('app volume active', events[1]['message'])
        self.assertFalse(self.pending.exists())

    def test_failed_recovery_retains_record_and_reports_failure(self):
        self.write_pending()
        with mock.patch.object(reefy_backup, '_snapshot_origin_suspended',
                               side_effect=RuntimeError('synthetic state failure')), \
                mock.patch.object(reefy_backup, 'publish_status') as publish:
            with self.assertRaisesRegex(RuntimeError, 'synthetic state failure'):
                reefy_backup.recover_pending_snapshot()
        self.assertTrue(self.pending.exists())
        self.assertIn('recovery failed', publish.call_args.args[0]['message'])

    def test_queued_backup_recovers_before_reading_config(self):
        events = []
        with mock.patch.object(reefy_backup, 'backup_lock'), \
                mock.patch.object(reefy_backup, 'recover_pending_snapshot',
                                  side_effect=lambda: events.append('recover')), \
                mock.patch.object(reefy_backup, 'run_backups',
                                  side_effect=lambda: events.append('run')):
            reefy_backup.main()
        self.assertEqual(events, ['recover', 'run'])

    def test_manual_backup_uses_systemd_cleanup_and_preserves_arguments(self):
        with mock.patch.object(reefy_backup.subprocess, 'call', return_value=1) as call:
            self.assertEqual(reefy_backup.run_supervised(['synthetic-instance']), 1)
        cmd = call.call_args.args[0]
        self.assertIn('--wait', cmd)
        self.assertIn('--service-type=oneshot', cmd)
        self.assertIn('--property=ExecStopPost=/usr/bin/env '
                      'REEFY_BACKUP_PHASE=cleanup /usr/bin/reefy-backup', cmd)
        self.assertEqual(cmd[-2:], ['/usr/bin/reefy-backup', 'synthetic-instance'])


class BorgReadinessTests(unittest.TestCase):
    def test_repo_readiness_preserves_original_six_hour_tolerance(self):
        self.assertEqual(reefy_backup.REPO_ACCESS_DEADLINE_S, 21600)
        self.assertEqual(reefy_backup.REPO_INIT_DEADLINE_S, 21600)
        self.assertEqual(reefy_backup.REPO_INFO_ATTEMPT_TIMEOUT_S, 21600)
        self.assertEqual(reefy_backup.REPO_INIT_ATTEMPT_TIMEOUT_S, 21600)
        self.assertEqual(reefy_backup.REPO_MAX_ATTEMPTS, 10)
        self.assertEqual(reefy_backup.REPO_RETRY_DELAY_S, 60)

    def test_dns_recovers_then_missing_repo_is_initialized(self):
        repo = 'ssh://backup.invalid/./synthetic-repository'
        env = {'BORG_RSH': 'synthetic'}
        responses = [
            (False, '', 'Temporary failure in name resolution'),
            (False, '', f'Repository {repo} does not exist.'),
            (True, '', ''),
        ]

        with mock.patch.object(
                reefy_backup, 'run_borg', side_effect=responses) as run_borg, \
                mock.patch.object(
                    reefy_backup.time, 'monotonic',
                    side_effect=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0]), \
                mock.patch.object(reefy_backup.time, 'sleep') as sleep, \
                mock.patch.object(reefy_backup, 'publish_status') as publish:
            ready = reefy_backup.ensure_repo_ready(
                repo, env, 'instance-synthetic'
            )

        self.assertTrue(ready)
        self.assertEqual(
            [call.args[0] for call in run_borg.call_args_list],
            [
                ['info', repo],
                ['info', repo],
                ['init', '--encryption=repokey-blake2', repo],
            ],
        )
        self.assertEqual(
            [call.kwargs['timeout'] for call in run_borg.call_args_list],
            [
                reefy_backup.REPO_INFO_ATTEMPT_TIMEOUT_S,
                reefy_backup.REPO_INFO_ATTEMPT_TIMEOUT_S,
                reefy_backup.REPO_INIT_ATTEMPT_TIMEOUT_S,
            ],
        )
        sleep.assert_called_once_with(reefy_backup.REPO_RETRY_DELAY_S)
        publish.assert_not_called()

    def test_info_timeout_publishes_existing_repo_access_error(self):
        repo = 'ssh://backup.invalid/./synthetic-repository'
        timeout = reefy_backup.subprocess.TimeoutExpired(
            cmd=['borg', 'info', repo],
            timeout=1,
            stderr=b'synthetic command timeout',
        )

        with mock.patch.object(
                reefy_backup, 'REPO_ACCESS_DEADLINE_S', 1), \
                mock.patch.object(
                    reefy_backup.time, 'monotonic',
                    side_effect=[0.0, 0.0, 2.0]), \
                mock.patch.object(
                    reefy_backup.subprocess, 'run', side_effect=timeout), \
                mock.patch.object(reefy_backup.time, 'sleep') as sleep, \
                mock.patch.object(reefy_backup, 'publish_status') as publish:
            ready = reefy_backup.ensure_repo_ready(
                repo, {'BORG_RSH': 'synthetic'}, 'instance-synthetic'
            )

        self.assertFalse(ready)
        sleep.assert_not_called()
        publish.assert_called_once_with({
            'instance_uuid': 'instance-synthetic',
            'status': 'error',
            'message': 'repo access failed',
        })

    def test_init_timeout_publishes_existing_repo_init_error(self):
        repo = 'ssh://backup.invalid/./synthetic-repository'
        missing = types.SimpleNamespace(
            returncode=2,
            stdout='',
            stderr=f'Repository {repo} does not exist.',
        )
        timeout = reefy_backup.subprocess.TimeoutExpired(
            cmd=['borg', 'init', repo],
            timeout=1,
            stderr=b'synthetic command timeout',
        )

        with mock.patch.object(
                reefy_backup, 'REPO_INIT_DEADLINE_S', 1), \
                mock.patch.object(
                    reefy_backup.time, 'monotonic',
                    side_effect=[0.0, 0.0, 0.0, 0.0, 2.0]), \
                mock.patch.object(
                    reefy_backup.subprocess, 'run',
                    side_effect=[missing, timeout]), \
                mock.patch.object(reefy_backup.time, 'sleep') as sleep, \
                mock.patch.object(reefy_backup, 'publish_status') as publish:
            ready = reefy_backup.ensure_repo_ready(
                repo, {'BORG_RSH': 'synthetic'}, 'instance-synthetic'
            )

        self.assertFalse(ready)
        sleep.assert_not_called()
        publish.assert_called_once_with({
            'instance_uuid': 'instance-synthetic',
            'status': 'error',
            'message': 'repo init failed',
        })

    def test_permanent_identity_and_passphrase_errors_are_fatal(self):
        for message in (
                'Passphrase supplied in BORG_PASSPHRASE is incorrect',
                'Warning: Identity file /synthetic/key not accessible',
                'Invalid location format: synthetic'):
            with self.subTest(message=message):
                self.assertEqual(
                    reefy_backup._repo_error_kind(message), 'fatal')


class PublishStatusTests(unittest.TestCase):
    def test_nonzero_publish_is_retried(self):
        results = [
            types.SimpleNamespace(returncode=1),
            types.SimpleNamespace(returncode=0),
        ]
        with mock.patch.object(
                reefy_backup.subprocess, 'run', side_effect=results) as run, \
                mock.patch.object(reefy_backup.time, 'sleep') as sleep:
            published = reefy_backup.publish_status({
                'instance_uuid': 'instance-synthetic',
                'status': 'success',
            })

        self.assertTrue(published)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(
            reefy_backup.STATUS_PUBLISH_RETRY_DELAY_S)

    def test_publish_command_errors_exhaust_bounded_retries(self):
        with mock.patch.object(
                reefy_backup.subprocess, 'run',
                side_effect=OSError('synthetic command failure')) as run, \
                mock.patch.object(reefy_backup.time, 'sleep') as sleep:
            published = reefy_backup.publish_status({
                'instance_uuid': 'instance-synthetic',
                'status': 'error',
            })

        self.assertFalse(published)
        self.assertEqual(run.call_count,
                         reefy_backup.STATUS_PUBLISH_ATTEMPTS)
        self.assertEqual(
            sleep.call_count, reefy_backup.STATUS_PUBLISH_ATTEMPTS - 1)


class SnapshotReleaseTests(unittest.TestCase):
    def test_lv_removed_even_when_unmount_times_out(self):
        timeout = reefy_backup.subprocess.TimeoutExpired(
            cmd=['umount', '/synthetic/snapshot'], timeout=15)
        removed = types.SimpleNamespace(returncode=0, stderr='')
        with mock.patch.object(
                reefy_backup.subprocess, 'run',
                side_effect=[timeout, removed]) as run, \
                mock.patch.object(
                    reefy_backup.os.path, 'isdir', return_value=False):
            released = reefy_backup.release_snapshot(
                'reefy_snap_synthetic', '/synthetic/snapshot')

        self.assertTrue(released)
        self.assertEqual(run.call_count, 2)

    def test_failed_lvremove_reports_unreleased_snapshot(self):
        unmounted = types.SimpleNamespace(returncode=0, stderr='')
        not_removed = types.SimpleNamespace(
            returncode=5, stderr='synthetic busy LV')
        still_present = types.SimpleNamespace(
            returncode=0, stdout=' reefy_snap_synthetic\n', stderr='')
        with mock.patch.object(
                reefy_backup.subprocess, 'run',
                side_effect=[unmounted, not_removed, still_present]), \
                mock.patch.object(
                    reefy_backup.os.path, 'isdir', return_value=False):
            released = reefy_backup.release_snapshot(
                'reefy_snap_synthetic', '/synthetic/snapshot')

        self.assertFalse(released)

    def test_repeated_release_succeeds_when_lvm_confirms_absence(self):
        missing_mount = types.SimpleNamespace(
            returncode=32, stderr='synthetic not mounted')
        missing_lv = types.SimpleNamespace(
            returncode=5, stderr='synthetic LV not found')
        absent = types.SimpleNamespace(returncode=0, stdout='', stderr='')
        with mock.patch.object(
                reefy_backup.subprocess, 'run',
                side_effect=[missing_mount, missing_lv, absent]), \
                mock.patch.object(
                    reefy_backup.os.path, 'isdir', return_value=False):
            released = reefy_backup.release_snapshot(
                'reefy_snap_synthetic', '/synthetic/snapshot')

        self.assertTrue(released)

    def test_lvremove_timeout_succeeds_when_lvm_confirms_absence(self):
        unmounted = types.SimpleNamespace(returncode=0, stderr='')
        timeout = reefy_backup.subprocess.TimeoutExpired(
            cmd=['lvremove'], timeout=15)
        absent = types.SimpleNamespace(returncode=0, stdout='', stderr='')
        with mock.patch.object(
                reefy_backup.subprocess, 'run',
                side_effect=[unmounted, timeout, absent]), \
                mock.patch.object(
                    reefy_backup.os.path, 'isdir', return_value=False):
            released = reefy_backup.release_snapshot(
                'reefy_snap_synthetic', '/synthetic/snapshot')

        self.assertTrue(released)


class MaintenanceDeadlineTests(unittest.TestCase):
    def test_expired_deadline_skips_borg_command(self):
        with mock.patch.object(
                reefy_backup.time, 'monotonic', return_value=11), \
                mock.patch.object(reefy_backup, 'run_borg') as run:
            ok, _, error = reefy_backup.run_maintenance_borg(
                ['compact', 'synthetic-repository'], {}, timeout=300,
                deadline=10)

        self.assertFalse(ok)
        self.assertEqual(error, 'maintenance deadline reached')
        run.assert_not_called()


class BackupCompletionTests(unittest.TestCase):
    def test_missing_ssh_key_publishes_terminal_error(self):
        inst = {
            'instance_uuid': 'instance-synthetic',
            'archive_prefix': 'synthetic-app',
            'repo_path': 'ssh://backup.invalid/./synthetic-repository',
            'passphrase': 'synthetic-passphrase',
            'paths': ['/synthetic/data'],
        }
        with mock.patch.object(
                reefy_backup.os.path, 'exists', return_value=False), \
                mock.patch.object(reefy_backup, 'publish_status') as publish:
            succeeded = reefy_backup.backup_instance(inst, keep_last=3)

        self.assertFalse(succeeded)
        publish.assert_called_once_with({
            'instance_uuid': 'instance-synthetic',
            'status': 'error',
            'message': 'backup SSH key missing',
        })

    def test_success_survives_maintenance_failures(self):
        events = []
        captured_env = {}
        maintenance_kwargs = {}

        def ensure_ready(repo_path, env, instance_uuid):
            events.append('ready')
            captured_env.update(env)
            return True

        def run_borg(args, env, **kwargs):
            command = args[0]
            events.append(command)
            if command == 'create':
                return (
                    True,
                    '{"archive":{"stats":{"compressed_size":321}}}',
                    '',
                )
            maintenance_kwargs[command] = kwargs
            if command == 'prune':
                raise RuntimeError('synthetic prune failure')
            return False, '', 'synthetic compact failure'

        def release_snapshot(snap_name, snap_mnt):
            events.append('release')
            return True

        statuses = []

        def publish_status(payload):
            events.append('publish')
            statuses.append(payload)
            return True

        inst = {
            'instance_uuid': 'instance-synthetic',
            'archive_prefix': 'synthetic-app',
            'repo_path': 'ssh://backup.invalid/./synthetic-repository',
            'passphrase': 'synthetic-passphrase',
            'paths': ['/mnt/reefy-data/apps/instance-synthetic/data'],
        }

        with mock.patch.object(
                reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(
                    reefy_backup, 'ensure_repo_ready',
                    side_effect=ensure_ready), \
                mock.patch.object(
                    reefy_backup, 'snapshot_volume',
                    return_value=(
                        'reefy_snap_synthetic',
                        '/synthetic/snapshot/data',
                    )), \
                mock.patch.object(
                    reefy_backup, 'release_snapshot',
                    side_effect=release_snapshot), \
                mock.patch.object(
                    reefy_backup, 'run_borg', side_effect=run_borg), \
                mock.patch.object(
                    reefy_backup, 'publish_status',
                    side_effect=publish_status), \
                mock.patch.object(
                    reefy_backup.time, 'monotonic',
                    side_effect=[10.0, 15.0, 15.0, 15.0, 15.0]), \
                mock.patch.object(reefy_backup.time, 'time', return_value=1234):
            succeeded = reefy_backup.backup_instance(inst, keep_last=3)

        self.assertTrue(succeeded)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]['status'], 'success')
        self.assertEqual(statuses[0]['instance_uuid'], 'instance-synthetic')
        self.assertEqual(statuses[0]['duration_s'], 5)
        self.assertEqual(statuses[0]['size_bytes'], 321)
        self.assertLess(events.index('release'), events.index('publish'))
        self.assertLess(events.index('publish'), events.index('prune'))
        self.assertLess(events.index('publish'), events.index('compact'))
        self.assertIn('prune', events)
        self.assertIn('compact', events)
        self.assertEqual(maintenance_kwargs['prune']['retries'], 1)
        self.assertEqual(maintenance_kwargs['compact']['retries'], 1)

        borg_rsh = captured_env['BORG_RSH']
        for option in (
                '-o ConnectTimeout=15',
                '-o ConnectionAttempts=1',
                '-o ServerAliveInterval=15',
                '-o ServerAliveCountMax=2'):
            self.assertIn(option, borg_rsh)

    def test_unreleased_snapshot_prevents_success_publication(self):
        statuses = []
        inst = {
            'instance_uuid': 'instance-synthetic',
            'archive_prefix': 'synthetic-app',
            'repo_path': 'ssh://backup.invalid/./synthetic-repository',
            'passphrase': 'synthetic-passphrase',
            'paths': ['/mnt/reefy-data/apps/instance-synthetic/data'],
        }

        with mock.patch.object(
                reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(
                    reefy_backup, 'ensure_repo_ready', return_value=True), \
                mock.patch.object(
                    reefy_backup, 'snapshot_volume',
                    return_value=(
                        'reefy_snap_synthetic', '/synthetic/snapshot/data')), \
                mock.patch.object(
                    reefy_backup, 'release_snapshot', return_value=False), \
                mock.patch.object(
                    reefy_backup, 'run_borg',
                    return_value=(
                        True,
                        '{"archive":{"stats":{"compressed_size":1}}}',
                        '')), \
                mock.patch.object(
                    reefy_backup, 'publish_status',
                    side_effect=lambda payload: statuses.append(payload)), \
                mock.patch.object(
                    reefy_backup.time, 'monotonic', return_value=10), \
                mock.patch.object(reefy_backup.time, 'time', return_value=1234):
            succeeded = reefy_backup.backup_instance(inst, keep_last=3)

        self.assertFalse(succeeded)
        self.assertEqual([status['status'] for status in statuses], ['error'])
        self.assertEqual(statuses[0]['message'], 'snapshot release failed')

    def test_unpublished_success_skips_maintenance_and_returns_failure(self):
        commands = []

        def run_borg(args, env, **kwargs):
            commands.append(args[0])
            if args[0] == 'create':
                return (
                    True,
                    '{"archive":{"stats":{"compressed_size":1}}}',
                    '',
                )
            return True, '', ''

        inst = {
            'instance_uuid': 'instance-synthetic',
            'archive_prefix': 'synthetic-app',
            'repo_path': 'ssh://backup.invalid/./synthetic-repository',
            'passphrase': 'synthetic-passphrase',
            'paths': ['/mnt/reefy-data/apps/instance-synthetic/data'],
        }

        with mock.patch.object(
                reefy_backup.os.path, 'exists', return_value=True), \
                mock.patch.object(
                    reefy_backup, 'ensure_repo_ready', return_value=True), \
                mock.patch.object(
                    reefy_backup, 'snapshot_volume',
                    return_value=(
                        'reefy_snap_synthetic', '/synthetic/snapshot/data')), \
                mock.patch.object(
                    reefy_backup, 'release_snapshot', return_value=True), \
                mock.patch.object(
                    reefy_backup, 'run_borg', side_effect=run_borg), \
                mock.patch.object(
                    reefy_backup, 'publish_status', return_value=False), \
                mock.patch.object(
                    reefy_backup.time, 'monotonic', return_value=10), \
                mock.patch.object(reefy_backup.time, 'time', return_value=1234):
            succeeded = reefy_backup.backup_instance(inst, keep_last=3)

        self.assertFalse(succeeded)
        self.assertEqual(commands, ['create'])


if __name__ == '__main__':
    unittest.main()
