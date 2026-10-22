"""Download exclusion must never conceal overlapping or unmeasured work."""
import json
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
import _bootstrap  # noqa: F401
from reefy import startup_timing as t


def records(*spans):
    result = []
    for number, (start, end, kind, parent) in enumerate(spans):
        key = f'{number + 1:032x}'
        result += [{'event': 'start', 'id': key,
                    'parent': f'{parent + 1:032x}' if parent is not None else None,
                    'component': 'artifact_download' if kind == 'download' else 'reconciliation',
                    'kind': kind, 'at': start},
                   {'event': 'end', 'id': key, 'at': end, 'succeeded': True}]
    return result


def test_serial_transfer_excludes_only_network_not_verification_or_activation():
    evidence = records((10, 90, 'local', None), (20, 50, 'download', 0))
    r = t.summarize(evidence, 100)
    assert r['download_only_wait_s'] == 30
    assert r['adjusted_ready_s'] == 70
    assert sum(r['components_s'].values()) == 100
    assert r['components_s']['reconciliation'] == 50


def test_parallel_downloads_are_unioned_not_summed():
    evidence = records((10, 90, 'wait', None),
                       (20, 60, 'download', 0), (40, 80, 'download', 0))
    assert t.summarize(evidence, 100)['download_only_wait_s'] == 60


def test_overlap_with_local_work_keeps_the_clock_running():
    evidence = records((10, 90, 'wait', None), (20, 80, 'download', 0),
                       (30, 70, 'local', 0))
    r = t.summarize(evidence, 100)
    assert r['download_only_wait_s'] == 20
    assert r['adjusted_ready_s'] == 80


def test_independent_cached_provider_activation_is_local_work():
    evidence = records((10, 90, 'local', None), (20, 80, 'download', 0),
                       (0, 100, 'local', None))
    assert t.summarize(evidence, 100)['download_only_wait_s'] == 0


def test_waiting_consumers_do_not_count_shared_download_twice():
    evidence = records((10, 90, 'wait', None), (20, 80, 'download', 0),
                       (15, 85, 'wait', 0), (30, 75, 'wait', 0))
    assert t.summarize(evidence, 100)['download_only_wait_s'] == 60


def test_failed_verification_invalidates_download_credit_in_its_subtree():
    evidence = records((10, 90, 'local', None), (20, 80, 'download', 0))
    evidence[1]['succeeded'] = False
    assert t.summarize(evidence, 100)['download_only_wait_s'] == 0


def test_failed_transfer_receives_no_credit():
    evidence = records((10, 90, 'local', None), (20, 80, 'download', 0))
    evidence[-1]['succeeded'] = False
    assert t.summarize(evidence, 100)['download_only_wait_s'] == 0


def test_post_ready_activity_does_not_change_first_ready_measurement():
    evidence = records((10, 90, 'local', None), (20, 80, 'download', 0),
                       (110, 200, 'download', None))
    assert t.summarize(evidence, 100)['download_only_wait_s'] == 60


@pytest.mark.parametrize('bad', ['truncated', 'negative', 'duplicate', 'orphan', 'cycle'])
def test_invalid_evidence_fails_closed(bad):
    evidence = records((10, 90, 'local', None), (20, 80, 'download', 0))
    if bad == 'truncated':evidence.pop()
    if bad == 'negative':evidence[-1]['at'] = -1
    if bad == 'duplicate':evidence.append(evidence[-1])
    if bad == 'orphan':evidence[2]['parent'] = 'f' * 32
    if bad == 'cycle':evidence[0]['parent'] = evidence[2]['id']
    with pytest.raises(ValueError):t.summarize(evidence, 100)


def test_missing_or_corrupt_file_falls_back_to_full_raw_time(tmp_path, monkeypatch):
    monkeypatch.setattr(t, 'PATH', str(tmp_path / 'spans'))
    for value in (None, 'broken\n', json.dumps({'event': 'start'})+'\n'):
        if value is not None:(tmp_path / 'spans').write_text(value)
        r = t.collect(100)
        assert r['status'] == 'unavailable'
        assert r['adjusted_ready_s'] == 100
        assert r['download_only_wait_s'] == 0


def test_threaded_recording_contains_no_arguments_or_environment(tmp_path, monkeypatch):
    path = tmp_path/'spans';monkeypatch.setattr(t, 'PATH', str(path))
    def action(secret):
        with t.scope('artifact_download', kind='download'):
            return secret
    with t.scope('reconciliation', root=True):
        with t.scope('applications', kind='wait'), ThreadPoolExecutor(2) as pool:
            futures = [t.submit(pool, action, 'synthetic-secret') for _ in range(2)]
            assert all(f.result() == 'synthetic-secret' for f in futures)
    data = path.read_text()
    assert 'synthetic-secret' not in data
    rows = [json.loads(line) for line in data.splitlines()]
    r = t.summarize(rows, max(x['at'] for x in rows)+1)
    assert r['status'] == 'complete'


def test_progress_excludes_observed_download_only_not_unpack(tmp_path, monkeypatch):
    path=tmp_path/'spans';monkeypatch.setattr(t,'PATH',str(path))
    with mock.patch.object(t.time,'monotonic',side_effect=[0,10,20,30,40,50,60,70]):
        with t.scope('image_pull',root=True):
            m=t.ImageDownloads()
            m.feed('aabbccddeeff Pulling fs layer')
            m.feed('aabbccddeeff Downloading 1MB/3MB')
            m.feed('aabbccddeeff Downloading 3MB/3MB')
            m.feed('aabbccddeeff Verifying Checksum')
            m.feed('aabbccddeeff Extracting')
            m.feed('aabbccddeeff Pull complete')
            m.close(True)
    r=t.summarize([json.loads(x) for x in path.read_text().splitlines()],80)
    assert r['download_only_wait_s']==10
    assert r['adjusted_ready_s']==70


def test_failed_or_unknown_image_progress_gets_no_credit(tmp_path, monkeypatch):
    path=tmp_path/'spans';monkeypatch.setattr(t,'PATH',str(path))
    with t.scope('image_pull',root=True):
        m=t.ImageDownloads();m.feed('unrecognized output');m.close(True)
        m=t.ImageDownloads();m.feed('aabbccddeeff Downloading 1MB/3MB')
        m.feed('aabbccddeeff Downloading 3MB/3MB');m.close(False)
    rows=[json.loads(x) for x in path.read_text().splitlines()]
    assert t.summarize(rows,max(x['at'] for x in rows)+1)['download_only_wait_s']==0


def test_subprocess_spans_remain_attached_to_parent(tmp_path,monkeypatch):
    import subprocess,sys,os
    path=tmp_path/'spans';monkeypatch.setattr(t,'PATH',str(path))
    library=str(__import__('pathlib').Path(t.__file__).parents[1])
    code=("import time; from reefy import startup_timing as t; t.PATH="+repr(str(path))+"\n"
          "with t.scope('artifact_preparation',parent=t.inherited_parent(),root=True):\n"
          " with t.scope('artifact_download',kind='download'): time.sleep(0.01)\n")
    with t.scope('reconciliation',root=True):
        env=t.subprocess_env();env['PYTHONPATH']=library
        subprocess.run([sys.executable,'-c',code],env=env,check=True)
    result=t.collect(t.time.monotonic())
    assert result['status']=='complete'
    assert result['download_only_wait_s']>0
    assert '_REEFY_TIMING_PARENT' not in path.read_text()


def test_wrong_boot_and_full_trace_receive_no_credit(tmp_path,monkeypatch):
    path=tmp_path/'spans';monkeypatch.setattr(t,'PATH',str(path))
    with t.scope('reconciliation',root=True):
        with t.scope('artifact_download',kind='download'):pass
    data=path.read_text().replace(t.BOOT_ID,'synthetic-other-boot')
    path.write_text(data)
    assert t.collect(100)['download_only_wait_s']==0
    path.write_text(' '*t.MAX_BYTES)
    assert t.collect(100)['status']=='unavailable'


def test_image_extraction_overlapping_download_is_not_deducted(tmp_path,monkeypatch):
    path=tmp_path/'spans';monkeypatch.setattr(t,'PATH',str(path))
    with t.scope('image_pull',root=True):
        m=t.ImageDownloads()
        for line in ('aabbccddeeff Pulling fs layer','112233445566 Pulling fs layer',
                     '112233445566 Extracting','aabbccddeeff Downloading 1MB',
                     'aabbccddeeff Downloading 2MB','aabbccddeeff Pull complete',
                     '112233445566 Pull complete'):
            m.feed(line)
        m.close(True)
    assert t.collect(t.time.monotonic())['download_only_wait_s']==0


def test_first_ready_seals_new_spans_but_pending_spans_can_finish(tmp_path,monkeypatch):
    path=tmp_path/'spans';monkeypatch.setattr(t,'PATH',str(path))
    with t.scope('reconciliation',root=True):
        with t.scope('artifact_download',kind='download'):pass
        t.seal()
        count=len(path.read_text().splitlines())
        with t.scope('provider_activation'):pass
        assert len(path.read_text().splitlines())==count
    assert t.collect(t.time.monotonic())['status']=='complete'
    final=path.read_bytes()
    with t.scope('reconciliation',root=True):
        with t.scope('artifact_download',kind='download'):pass
    assert path.read_bytes()==final


def test_trace_writes_are_not_misclassified_as_download_wait(tmp_path,monkeypatch):
    path=tmp_path/'spans';monkeypatch.setattr(t,'PATH',str(path))
    clock=[0.0];monkeypatch.setattr(t.time,'monotonic',lambda:clock[0])
    emit=t._emit
    def expensive_trace(row):
        clock[0]+=2
        return emit(row)
    monkeypatch.setattr(t,'_emit',expensive_trace)
    with t.scope('reconciliation',root=True):
        with t.scope('artifact_download',kind='download'):
            clock[0]+=5
    result=t.collect(clock[0]+1)
    assert result['download_only_wait_s']==5
    assert result['adjusted_ready_s']==clock[0]+1-5
