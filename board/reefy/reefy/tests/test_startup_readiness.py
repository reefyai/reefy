"""Concurrency and retry invariants for bounded startup preparation."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import _bootstrap  # noqa: F401
from reefy import artifacts, dataplane
from test_dataplane import _make_dp
from test_artifacts import _manager


def test_identical_apply_retries_share_running_and_pending_jobs():
    dp = _make_dp()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def apply(payload):
        calls.append(payload['state'])
        entered.set()
        assert release.wait(5)
        return True

    with mock.patch.object(dp, '_apply_state', side_effect=apply):
        first = dp._submit_apply_job('apply', state={'value': 'first'})
        assert entered.wait(5)
        try:
            repeat = dp._submit_apply_job('apply', state={'value': 'first'})
            assert repeat['request_id'] == first['request_id']
            pending = dp._submit_apply_job('apply', state={'value': 'next'})
            same_pending = dp._submit_apply_job('apply', state={'value': 'next'})
            assert same_pending['request_id'] == pending['request_id']
        finally:
            with dp._job_condition:
                queued = dp._pending_job
            release.set()
            dp._wait_apply_result(first['request_id'])
            if queued:
                dp._wait_apply_result(queued['request_id'])
        assert dp._wait_apply_result(pending['request_id'])['status'] == 'succeeded'
    assert calls == [{'value': 'first'}, {'value': 'next'}]


def test_reverting_pending_state_does_not_reuse_superseded_running_work():
    dp = _make_dp()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def apply(payload):
        calls.append(payload['state'])
        entered.set()
        assert release.wait(5)
        return True

    with mock.patch.object(dp, '_apply_state', side_effect=apply):
        first = dp._submit_apply_job('apply', state={'value': 'first'})
        assert entered.wait(5)
        try:
            pending = dp._submit_apply_job('apply', state={'value': 'next'})
            revert = dp._submit_apply_job('apply', state={'value': 'first'})
            assert revert['request_id'] != first['request_id']
            assert dp._get_apply_result(pending['request_id'])['status'] == 'superseded'
        finally:
            with dp._job_condition:
                queued = dp._pending_job
            release.set()
            dp._wait_apply_result(first['request_id'])
            if queued:
                dp._wait_apply_result(queued['request_id'])
        assert dp._wait_apply_result(revert['request_id'])['status'] == 'succeeded'
    assert calls == [{'value': 'first'}, {'value': 'first'}]


def test_failed_completed_apply_can_be_retried():
    dp = _make_dp()
    with mock.patch.object(dp, '_apply_state', side_effect=[False, True]) as apply:
        first = dp._submit_apply_job('apply', state={'value': 'same'})
        assert dp._wait_apply_result(first['request_id'])['status'] == 'failed'
        second = dp._submit_apply_job('apply', state={'value': 'same'})
        assert dp._wait_apply_result(second['request_id'])['status'] == 'succeeded'
    assert first['request_id'] != second['request_id']
    assert apply.call_count == 2


def test_shared_failure_preserves_each_apps_requirement_and_retries_next_pass():
    dp = _make_dp()
    reference = 'example.invalid/provider@sha256:' + 'a' * 64
    optional = {'ref': reference, 'kind': 'host-extension', 'required': False}
    required = {**optional, 'required': True}
    batch = dataplane._SharedPreparation()
    with mock.patch.object(dataplane.subprocess, 'run', return_value=mock.Mock(
            returncode=1, stdout='', stderr='synthetic failure')) as run:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(dp._prepare_one_app_artifact,
                                   {'project_name': f'synthetic-{index}'},
                                   required if index == 0 else optional,
                                   preparation=batch) for index in range(6)]
            assert [future.result() for future in futures] == [False] + [True] * 5
        assert run.call_count == 1
        run.return_value = mock.Mock(returncode=0, stdout='', stderr='')
        assert dp._prepare_one_app_artifact({}, required,
                                            preparation=dataplane._SharedPreparation())
        assert run.call_count == 2


def test_shared_preparation_propagates_exception_without_stranding_waiters():
    batch = dataplane._SharedPreparation()
    def failure():
        raise RuntimeError('synthetic')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(batch.run, ('same',), failure) for _ in range(2)]
        for future in futures:
            try:
                future.result(timeout=2)
            except RuntimeError as error:
                assert str(error) == 'synthetic'
            else:
                raise AssertionError('Shared failure was swallowed')


def test_cached_activation_deduplicates_but_next_boot_pass_retries(tmp_path):
    manager = _manager()
    artifact = {'ref': 'oci:///synthetic@sha256:' + 'a' * 64,
                'kind': 'host-extension'}
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'apps': [{'artifacts': [artifact]} for _ in range(6)]}))
    with mock.patch.object(manager, 'prepare', side_effect=artifacts.ArtifactError('synthetic')) as prepare:
        manager.activate_cached_state(str(state))
        assert prepare.call_count == 1
        manager.activate_cached_state(str(state))
        assert prepare.call_count == 2


def test_apps_share_settle_wait_but_device_readiness_is_rechecked():
    dp = _make_dp()
    batch = dataplane._SharedPreparation()
    app = {'artifacts': [{'ref': 'example.invalid/provider@sha256:' + 'a' * 64}]}
    compose = {'services': {'app': {'devices': ['intel.com/gpu=all']}}}
    with mock.patch.object(dataplane, '_wait_for_requested_devices',
                           return_value=['intel.com/gpu=all']) as settle, \
            mock.patch.object(dataplane, '_missing_requested_devices',
                              side_effect=[['intel.com/gpu=all'], []]):
        first = dp._in_preparation_pass(batch, dp._settle_provider_devices,
                                        app, compose, ['intel.com/gpu=all'])
        second = dp._in_preparation_pass(batch, dp._settle_provider_devices,
                                         app, compose, ['intel.com/gpu=all'])
    assert first == ['intel.com/gpu=all']
    assert second == []
    assert settle.call_count == 1
    assert getattr(dp._preparation_local, 'value', None) is None


def test_distinct_provider_dependencies_get_independent_settle_waits():
    dp = _make_dp()
    batch = dataplane._SharedPreparation()
    with mock.patch.object(dataplane, '_wait_for_requested_devices') as settle, \
            mock.patch.object(dataplane, '_missing_requested_devices', return_value=[]):
        for name in ('first', 'second'):
            app = {'artifacts': [{'ref': 'example.invalid/' + name}]}
            dp._in_preparation_pass(batch, dp._settle_provider_devices,
                                    app, {}, ['intel.com/gpu=all'])
    assert settle.call_count == 2
