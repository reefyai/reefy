#!/usr/bin/env python3
"""Real Docker removal with a synthetic eligibility clock and references."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_admission import reservation
from reefy.storage_images import maintain
from reefy.storage_quota import atomic_json, command
from reefy.storage_runtime import LAYER_INITIAL_SIZE


def stamp(month, day):
    return datetime(2024, month, day, tzinfo=timezone.utc).timestamp()


def run():
    images, containers = {}, []
    try:
        for role in ('expired', 'stopped', 'pending', 'young', 'race'):
            with reservation('synthetic-retention-create', 2 * LAYER_INITIAL_SIZE):
                container = command(['docker', 'create', 'busybox:1.37.0', 'sleep', '3600']).strip()
            try:
                images[role] = command(['docker', 'commit', '--change', 'LABEL synthetic.retention=' + role, container]).strip()
            finally:
                command(['docker', 'rm', container])
        with reservation('synthetic-retention-stopped-reference', 2 * LAYER_INITIAL_SIZE):
            containers.append(command(['docker', 'create', images['stopped'], 'sleep', '3600']).strip())
        history = '/run/synthetic-retention-history.json'
        atomic_json(history, {'version': 1, 'last_observed': stamp(4, 29), 'images': {
            image: {'unreferenced_since': stamp(4, 1) if role == 'young' else stamp(1, 31)}
            for role, image in images.items()}})
        calls = 0

        def references():
            nonlocal calls
            calls += 1
            # A pending update becomes visible after the initial observation.
            return {images['pending']} | ({images['race']} if calls > 1 else set())

        removed = maintain(references=references, history_path=history,
                           now=stamp(4, 30), clock_trusted=True)
        assert removed == [images['expired']], removed
        present = set(command(['docker', 'image', 'ls', '-q', '--no-trunc']).split())
        assert images['expired'] not in present
        assert set(images.values()) - {images['expired']} <= present
        observed = json.loads(Path(history).read_text())['images']
        assert images['stopped'] not in observed
        assert images['pending'] not in observed
        assert images['race'] not in observed
        assert observed[images['young']]['unreferenced_since'] == stamp(4, 1)
        # An untrusted/rolled-back clock postpones deletion even with old data.
        assert maintain(references=lambda: set(), history_path=history,
                        now=stamp(4, 1), clock_trusted=False) == []
        print(json.dumps({'real_nonforce_removal': 'passed', 'calendar_month_expiry': 'passed',
                          'stopped_pending_and_raced_references': 'passed',
                          'young_cache_retained_and_untrusted_clock_postponed': 'passed'}))
    finally:
        for container in containers:
            command(['docker', 'rm', container])
        present = set(command(['docker', 'image', 'ls', '-q', '--no-trunc']).split())
        for image in set(images.values()) & present:
            command(['docker', 'image', 'rm', image])


if __name__ == '__main__':
    run()
