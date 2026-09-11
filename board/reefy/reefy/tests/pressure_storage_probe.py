#!/usr/bin/env python3
"""Concurrent class pressure on the dedicated dm-thin fixture, never host data.

Uses real quota counters, ten-second production Guard passes and independently
sampled physical usage. The rate-limited synthetic workload does not qualify
an unbounded hardware write rate or the separate snapshot-COW fault cases.
"""
from dataclasses import asdict
import errno
import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_guard import Guard
from reefy.storage_migration import Migration
from reefy.storage_quota import Registry, command, flush_filesystem, read_quotas
from thin_storage_probe import ROOT, sample, MIB, CHUNK


class Writer:
    def __init__(self, path):
        self.path = path
        self.bytes = self.errors = 0
        self.failure = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.write, daemon=True)
        self.thread.start()

    def write(self):
        try:
            with open(self.path, 'wb', buffering=0) as stream:
                block = b's' * MIB
                while not self.stop.is_set():
                    started = time.monotonic()
                    try:
                        written = stream.write(block)
                        os.fsync(stream.fileno())
                        self.bytes += written
                    except OSError as error:
                        if error.errno not in (errno.ENOSPC, errno.EDQUOT):
                            raise
                        self.errors += 1
                    # Eight MiB/s each, at most five concurrent writers. No
                    # compressed/zero discard shortcuts: each file allocates.
                    self.stop.wait(max(0, 0.125 - (time.monotonic() - started)))
        except Exception as error:
            self.failure = repr(error)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), 'synthetic writer did not stop'
        assert self.failure is None, self.failure


def run():
    # This fixture is established by thin_storage_probe after validating the
    # dedicated synthetic disk serial. Refuse any other mount/device identity.
    mounted = json.loads(command(['findmnt', '--json', '--target', ROOT,
                                  '-o', 'TARGET,SOURCE']))['filesystems'][0]
    assert mounted['target'] == ROOT
    assert os.path.realpath(mounted['source']) == os.path.realpath('/dev/quota_probe/data')
    for name in ('cow', 'no-discard', 'recordings'):
        Path(ROOT, 'media', name).unlink(missing_ok=True)
    command(['fstrim', ROOT], timeout=60)
    assert sample().used < 2 * 1024**3, asdict(sample())
    policies = {ROOT: 'runtime', ROOT + '/media': 'bulk', ROOT + '/state': 'state',
                ROOT + '/bulk-second': 'bulk', ROOT + '/runtime': 'runtime'}
    for path in policies:
        Path(path).mkdir(exist_ok=True)
    registry = Registry('/run/reefy/storage-pressure/synthetic-thin-registry.json')
    Migration(registry=registry, sample=sample).prepare(policies)
    # Root-directory housekeeping is bounded independently, so an idle root
    # cannot retain unconsumed startup runway during the class transition test.
    root_record = next(row for row in registry.data['projects'].values() if row['path'] == ROOT)
    root_record['max_hard'] = 4 * MIB
    registry.data.pop('peak_bytes_per_second', None)
    registry.save()
    guard = Guard(peak_bytes_per_second=128 * MIB, response_seconds=4,
                  in_flight_bytes=64 * MIB, registry_path=registry.path,
                  sample=sample, status_path='/run/synthetic-pressure-status.json')
    initial = guard.pass_once()
    reserve = initial['allocation']['boundaries']['emergency']
    trace, passes, writers = [], [], {}
    started = time.monotonic()
    next_pass = started + 10
    stage_writes = {}
    reached = False
    try:
        for path in policies:
            if path != ROOT:
                writers[path] = Writer(path + '/pressure-data')
        while time.monotonic() - started < 600:
            current = sample()
            trace.append({'seconds': time.monotonic() - started, **asdict(current)})
            assert current.healthy
            assert current.capacity - current.used >= reserve, trace[-1]
            assert current.metadata_used * 100 < current.metadata_capacity * 85
            assert not any(writer.failure for writer in writers.values())
            if time.monotonic() >= next_pass:
                status = guard.pass_once()
                passes.append(status)
                next_pass = time.monotonic() + 10
                stage = status['allocation']['stage']
                stage_writes.setdefault(stage, {path: writer.bytes for path, writer in writers.items()})
                if status['allocation']['quiesce']:
                    reached = True
                    break
            third = ROOT + '/bulk-third'
            if third not in writers and sum(writer.bytes for writer in writers.values()) >= 256 * MIB:
                Path(third).mkdir()
                policies[third] = 'bulk'
                # Empty destination is assigned and limited before its writer
                # exists. Existing completed domains are not recursively tagged.
                registry = Registry(registry.path)
                Migration(registry=registry, sample=sample).prepare(policies)
                guard.pass_once()
                writers[third] = Writer(third + '/pressure-data')
            time.sleep(0.25)
    finally:
        for writer in writers.values():
            writer.close()
        flush_filesystem(ROOT)
        Path('/tmp/synthetic-pressure-trace.json').write_text(json.dumps({
            'samples': trace, 'passes': passes, 'stage_writes': stage_writes,
            'writers': {path: {'bytes': writer.bytes, 'errors': writer.errors,
                                'failure': writer.failure} for path, writer in writers.items()}}))
    assert reached, {'stages': list(stage_writes), 'last': passes[-1] if passes else None}
    assert {'bulk', 'runtime', 'state'} <= set(stage_writes), stage_writes
    for path, writer in writers.items():
        assert writer.bytes > 64 * MIB, (path, writer.bytes)
        if policies[path] == 'bulk':
            assert writer.errors > 0, 'bulk writer never received allocation pressure'
    assert writers[ROOT + '/state'].bytes > stage_writes['state'][ROOT + '/state'] + 64 * MIB
    assert writers[ROOT + '/runtime'].bytes > stage_writes['runtime'][ROOT + '/runtime'] + 64 * MIB
    report = read_quotas(ROOT)
    assert all(quota['hard'] > 0 for project, quota in report.items() if project and quota['used'])
    print(json.dumps({'concurrent_bulk_runtime_state_and_dynamic_third_volume': 'passed',
                      'seconds': time.monotonic() - started,
                      'physical_high_water': max(row['used'] for row in trace),
                      'reserve_bytes': reserve, 'stages': list(stage_writes)}))


if __name__ == '__main__':
    run()
