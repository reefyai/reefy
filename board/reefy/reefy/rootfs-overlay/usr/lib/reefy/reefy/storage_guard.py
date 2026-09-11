"""Single-writer quota passes with independent physical-pool observations.

Lifecycle operations publish a complete inventory while writers are held.
This module does no recursive filesystem work and never deletes app data.
"""
from dataclasses import asdict
import time

from reefy.storage_pressure import Consumer, PressureError, allocate, apply_allocation
from reefy.storage_quota import (
    Registry, RUN_DIR, atomic_json, physical_sample, read_quotas,
    require_enforcement, set_quota, state_lock,
)


class Guard:
    def __init__(self, *, peak_bytes_per_second, response_seconds,
                 in_flight_bytes, registry_path=None, clock=time.monotonic,
                 sample=physical_sample):
        # An observed average is not a cold-start safety bound. The integration
        # must supply a validated physical rate/response envelope explicitly.
        if peak_bytes_per_second <= 0 or response_seconds <= 0 or in_flight_bytes < 0:
            raise ValueError('a validated positive physical response bound is required')
        self.peak = peak_bytes_per_second
        self.response = response_seconds
        self.in_flight = in_flight_bytes
        self.registry_path = registry_path
        self.clock, self.sample = clock, sample
        self.previous = {}
        self.previous_time = None
        self.previous_physical = None
        self.demand = {}

    def pass_once(self, *, pending_bytes=0):
        started = self.clock()
        with state_lock():
            registry = Registry(self.registry_path) if self.registry_path else Registry()
            if not registry.data.get('active'):
                raise PressureError('storage policy has not been activated')
            if not registry.data.get('inventory_complete'):
                raise PressureError('storage writer inventory is incomplete')
            records = registry.data['projects']
            if any(not r.get('complete') for r in records.values() if not r.get('retired')):
                raise PressureError('project migration is incomplete')
            sample = self.sample()
            elapsed = started - self.previous_time if self.previous_time is not None else None
            if elapsed and self.previous_physical is not None:
                observed = max(0, sample.used - self.previous_physical) / elapsed
                if observed > self.peak:
                    raise PressureError('physical allocation exceeded the validated response bound')
            reports = {}
            for record in records.values():
                if record.get('retired'):
                    continue
                mount = record['mount']
                if mount not in reports:
                    require_enforcement(mount)
                    reports[mount] = read_quotas(mount)
            consumers, by_key = [], {}
            accounted = set()
            for key, record in records.items():
                if record.get('retired'):
                    continue
                domain = record['mount'], record['project']
                if domain in accounted:
                    raise PressureError('duplicate project in physical ledger')
                accounted.add(domain)
                quota = reports[record['mount']].get(record['project'])
                if quota is None or quota['soft']:
                    raise PressureError('missing project or conflicting soft quota')
                rate = self.demand.get(key, 0)
                if elapsed and key in self.previous:
                    observed = max(0, quota['used'] - self.previous[key]) / elapsed
                    # Decay slowly so a short cleanup pause does not repeatedly
                    # reset a recorder to a tiny startup allowance.
                    rate = max(observed, rate * 0.98)
                self.demand[key] = rate
                consumers.append(Consumer(
                    key, record['storage_class'], quota['used'], quota['hard'],
                    int(rate), record.get('max_hard')))
                by_key[key] = record
            # Retired and Docker-owned projects remain physical consumers. Their
            # outstanding allowances cannot disappear from the shared ledger.
            for mount, report in reports.items():
                for project, quota in report.items():
                    if (mount, project) in accounted:
                        continue
                    if project == 0:
                        if quota['used']:
                            raise PressureError('unowned project-zero allocation in managed filesystem')
                        continue
                    key = f'{mount}:unregistered:{project}'
                    consumers.append(Consumer(key, 'runtime', quota['used'], quota['hard']))
                    by_key[key] = {'mount': mount, 'project': project}
            allocation = allocate(
                sample, consumers, pending_bytes=pending_bytes,
                peak_bytes_per_second=self.peak, response_seconds=self.response,
                in_flight_bytes=self.in_flight)

            def deadline():
                if self.clock() - started > 20:
                    raise PressureError('quota pass deadline exceeded')

            def set_limit(key, limit):
                deadline()
                record = by_key[key]
                set_quota(record['mount'], record['project'], limit)

            def read_limit(key):
                deadline()
                record = by_key[key]
                return read_quotas(record['mount']).get(record['project'], {}).get('hard')

            apply_allocation(consumers, allocation, set_limit, read_limit)
            deadline()
            # Only a completely successful pass refreshes liveness. Exceptions
            # leave the previous heartbeat for the independent watchdog.
            finished = self.clock()
            self.previous = {c.key: c.used for c in consumers}
            self.previous_time, self.previous_physical = started, sample.used
            result = {'sampled_monotonic': started, 'completed_monotonic': finished,
                      'elapsed_seconds': finished - started, 'sample': asdict(sample),
                      'allocation': asdict(allocation)}
            atomic_json(RUN_DIR + '/status.json', result)
            return result
