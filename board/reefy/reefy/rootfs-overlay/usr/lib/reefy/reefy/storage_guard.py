"""Single-writer quota passes with independent physical-pool observations.

Lifecycle operations publish a complete inventory while writers are held.
This module does no recursive filesystem work and never deletes app data.
"""
from dataclasses import asdict
import os
import time

from reefy.storage_pressure import Consumer, PressureError, QUANTUM, allocate, apply_allocation, admit_reservations
from reefy.storage_quota import (
    Registry, RUN_DIR, atomic_json, physical_sample, read_quotas,
    require_enforcement, set_quota, state_lock, mount_info,
)
from reefy.storage_runtime import LAYER_INITIAL_SIZE, LAYER_SIZE, active_native_projects


class Guard:
    def __init__(self, *, peak_bytes_per_second, response_seconds,
                 in_flight_bytes, registry_path=None, clock=time.monotonic,
                 sample=physical_sample, status_path=RUN_DIR + '/status.json'):
        # An observed average is not a cold-start safety bound. The integration
        # must supply a validated physical rate/response envelope explicitly.
        if peak_bytes_per_second <= 0 or response_seconds <= 0 or in_flight_bytes < 0:
            raise ValueError('a validated positive physical response bound is required')
        self.peak = peak_bytes_per_second
        self.response = response_seconds
        self.in_flight = in_flight_bytes
        self.registry_path = registry_path
        self.clock, self.sample = clock, sample
        self.status_path = status_path
        self.previous = {}
        self.previous_time = None
        self.previous_physical = None
        self.demand = {}
        self.burst_credit = in_flight_bytes

    def pass_once(self, *, pending_bytes=0):
        started = self.clock()
        with state_lock():
            registry = Registry(self.registry_path) if self.registry_path else Registry()
            if not registry.data.get('active'):
                raise PressureError('storage policy has not been activated')
            if not registry.data.get('inventory_complete'):
                raise PressureError('storage writer inventory is incomplete')
            records = registry.data['projects']
            self.peak = max(self.peak, registry.data.get('peak_bytes_per_second', self.peak))
            if any(not r.get('complete') for r in records.values() if not r.get('retired')):
                raise PressureError('project migration is incomplete')
            sample = self.sample()
            elapsed = started - self.previous_time if self.previous_time is not None else None
            if elapsed and self.previous_physical is not None:
                # The envelope is rate * elapsed + in-flight bytes. Admission
                # wakes can sample only milliseconds apart; charging an allowed
                # burst as a sustained rate permanently invents a huge reserve.
                observed = max(0, sample.used - self.previous_physical
                               - self.burst_credit) / elapsed
                if observed > self.peak:
                    # Never retain a disproven bound or shrink it to manufacture
                    # capacity. The enlarged envelope can deny further admission.
                    self.peak = int(observed * 2) + 1
                    registry.data['peak_bytes_per_second'] = self.peak
                    registry.save()
                # Carry one shared burst allowance across frequent admission
                # wakes. It replenishes only with elapsed time; every new sample
                # must not receive a fresh 64 MiB exemption.
                growth = max(0, sample.used - self.previous_physical)
                self.burst_credit = min(self.in_flight, max(0,
                    self.burst_credit + self.peak * elapsed - growth))
            # A managed container replacement can remove a previously
            # inventoried native layer. Its dquot remains in reports below, so
            # retiring the path never discards physical usage or allowance.
            changed = False
            for record in records.values():
                if (record.get('native_docker') and not record.get('retired')
                        and not os.path.lexists(record['path'])):
                    record['retired'] = True
                    changed = True
            if changed:
                registry.save()
            reports = {}
            for record in records.values():
                if record.get('retired'):
                    continue
                current = mount_info(record['path'])
                if (current['uuid'] != record['filesystem']
                        or current['maj:min'] != record['device']
                        or current['target'] != record['mount']):
                    raise PressureError('governed volume mount identity changed')
                mount = record['mount']
                if mount not in reports:
                    require_enforcement(mount)
                    reports[mount] = read_quotas(mount)
            live_native = active_native_projects(registry)
            creating = any(lease.get('target') is None
                           for lease in registry.data.get('leases', {}).values())
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
                if quota is None or (quota['soft'] and not record.get('native_docker')):
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
                    if not quota['used'] and not quota['hard']:
                        continue  # empty retired/probe dquot, no outstanding allowance
                    key = f'{mount}:unregistered:{project}'
                    docker = registry.data.get('docker') or {}
                    native = (project >= docker.get('base_project', 2**32) + 2
                              and any(r['mount'] == mount and r['filesystem'] == docker.get('filesystem')
                                      for r in records.values()))
                    # Empty dquots outlive removed layers. Keep them closed;
                    # only an observed root gets ordinary writable-layer runway.
                    # A managed create can appear between inventory and report,
                    # so preserve its small, already-reserved initial allowance.
                    native_maximum = (LAYER_SIZE if project in live_native else
                                      LAYER_INITIAL_SIZE if creating else QUANTUM)
                    maximum = max(quota['used'], native_maximum if native else QUANTUM)
                    consumers.append(Consumer(key, 'runtime', quota['used'], quota['hard'],
                                              max_hard=maximum))
                    by_key[key] = {'mount': mount, 'project': project, 'native_docker': native}
            leases = registry.data.get('leases', {})
            pending_bytes += sum(lease['bytes'] for lease in leases.values())
            allocation = allocate(
                sample, consumers, pending_bytes=pending_bytes,
                peak_bytes_per_second=self.peak, response_seconds=self.response,
                in_flight_bytes=self.in_flight)

            allocation, admitted = admit_reservations(
                allocation, sample, consumers, leases, margin=self.peak * self.response)

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

            # Docker initially sets soft==hard. Normalize its soft limit without
            # adding allowance; app-owned unexpected soft quotas are an error.
            for consumer in consumers:
                record = by_key[consumer.key]
                quota = reports[record['mount']][record['project']]
                if quota['soft'] and record.get('native_docker'):
                    target = min(consumer.hard, allocation.limits[consumer.key])
                    set_limit(consumer.key, target)
                    if read_limit(consumer.key) != target:
                        raise PressureError('Docker soft quota normalization failed')
            apply_allocation(consumers, allocation, set_limit, read_limit)
            deadline()
            # Only a completely successful pass refreshes liveness. Exceptions
            # leave the previous heartbeat for the independent watchdog.
            finished = self.clock()
            self.previous = {c.key: c.used for c in consumers}
            self.previous_time, self.previous_physical = started, sample.used
            result = {'sampled_monotonic': started, 'completed_monotonic': finished,
                      # The one-second observer must begin containment at the
                      # same response margin as the allocator, not wait until
                      # that margin has already been consumed by snapshot COW.
                      'physical_stop_bytes': max(0, allocation.boundaries.state
                          - max(sample.chunk_bytes, int(self.peak * self.response))
                          - pending_bytes - len(consumers) * sample.chunk_bytes),
                      'generation': registry.data.get('generation', 0),
                      'admitted_leases': admitted,
                      'elapsed_seconds': finished - started, 'sample': asdict(sample),
                      'allocation': asdict(allocation)}
            atomic_json(self.status_path, result)
            return result
