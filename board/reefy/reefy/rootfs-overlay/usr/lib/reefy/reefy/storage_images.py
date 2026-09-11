"""Bounded age-only image maintenance, separate from quota monitoring.

All Reefy pull/installation mutations must share image_lock. The Docker API is
the only deletion path; referenced images are never forcibly removed.
"""
import contextlib
import json
import os
import re
import time

from reefy.storage_pressure import PressureError
from reefy.storage_quota import STATE_DIR, RUN_DIR, atomic_json, command, state_lock
from reefy.storage_retention import observe


IMAGE_ID = re.compile(r'^sha256:[a-f0-9]{64}$')


def image_lock():
    # Distinct from the quota writer lock: Docker may be slow, but a retention
    # operation cannot stall a physical monitoring pass.
    return state_lock(RUN_DIR + '/images.lock', timeout=5)


def desired_images(state):
    """All app/platform Compose images, including stopped desired apps."""
    projects = [state.get('compose') or {}]
    if state.get('schema_version') == 2:
        projects = [(state.get('system_project') or {}).get('compose') or {}]
        projects.extend(app.get('compose') or {} for app in state.get('apps') or [])
    return {service['image'] for project in projects
            for service in (project.get('services') or {}).values()
            if service.get('image')}


def docker_inventory():
    images = set(command(['docker', 'image', 'ls', '--no-trunc', '--quiet']).split())
    if any(not IMAGE_ID.fullmatch(image) for image in images):
        raise PressureError('Docker image identity is not immutable')
    containers = command(['docker', 'container', 'ls', '--all', '--quiet', '--no-trunc']).split()
    referenced = set()
    for offset in range(0, len(containers), 100):
        rows = json.loads(command(['docker', 'container', 'inspect', *containers[offset:offset+100]]))
        referenced.update(row['Image'] for row in rows)
    return images, referenced


def resolve_references(references):
    resolved = set()
    for reference in sorted(references):
        # An unresolved reference postpones this pass. Treating an inspect
        # failure as "not in use" could delete an image during daemon recovery.
        rows = json.loads(command(['docker', 'image', 'inspect', reference]))
        if len(rows) != 1 or not IMAGE_ID.fullmatch(rows[0]['Id']):
            raise PressureError('ambiguous Docker image reference')
        resolved.add(rows[0]['Id'])
    return resolved


def maintain(*, references, history_path=STATE_DIR + '/image-retention.json',
             now=None, clock_trusted=None, max_removals=10):
    """Observe and remove expired identities, re-reading references per removal.

    references() supplies current, pending and chosen rollback references while
    holding the same short image mutation lock used by lifecycle operations.
    This function never takes a physical-pressure argument or shortens expiry.
    """
    now = time.time() if now is None else now
    if clock_trusted is None:
        clock_trusted = os.path.exists('/run/systemd/timesync/synchronized')
    try:
        with open(history_path) as source:
            history = json.load(source)
    except FileNotFoundError:
        history = {}
    with image_lock():
        images, containers = docker_inventory()
        protected = containers | resolve_references(references())
        updated, candidates = observe(history, images, protected, now, clock_trusted=clock_trusted)
        atomic_json(history_path, updated)
    removed = []
    for candidate in sorted(candidates)[:max_removals]:
        with image_lock():
            images, containers = docker_inventory()
            protected = containers | resolve_references(references())
            if candidate not in images or candidate in protected:
                continue
            command(['docker', 'image', 'rm', candidate])
            removed.append(candidate)
    return removed
