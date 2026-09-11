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


def resolve_references(requested):
    # A complete successful local inventory distinguishes a genuinely absent
    # pending image from a failed Docker read. Only the latter postpones cleanup.
    return {identity for identity in available_identities(requested)
            if IMAGE_ID.fullmatch(identity)}


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
    with image_lock():
        try:
            with open(history_path) as source:
                history = json.load(source)
        except FileNotFoundError:
            history = {}
        images, containers = docker_inventory()
        protected = containers | resolve_references(references())
        updated, candidates = observe(history, images, protected, now, clock_trusted=clock_trusted)
        atomic_json(history_path, updated)
    removed = []
    for candidate in sorted(candidates)[:max_removals]:
        with image_lock():
            images, containers = docker_inventory()
            protected = containers | resolve_references(references())
            with open(history_path) as source:
                history = json.load(source)
            updated, still_eligible = observe(history, images, protected, now,
                                              clock_trusted=clock_trusted)
            atomic_json(history_path, updated)
            if candidate not in still_eligible:
                continue
            command(['docker', 'image', 'rm', candidate])
            removed.append(candidate)
    return removed


REFERENCES_PATH = STATE_DIR + '/image-references.json'
HISTORY_PATH = STATE_DIR + '/image-retention.json'


def read_references():
    try:
        with open(REFERENCES_PATH) as source:
            return json.load(source)
    except FileNotFoundError:
        return {'current': [], 'rollback': [], 'pending': {}}


def references():
    ledger = read_references()
    result = set(ledger.get('current', [])) | set(ledger.get('rollback', []))
    from reefy.shared import load_desired_state
    try:
        state, _ = load_desired_state()
    except FileNotFoundError:
        state = {}
    result.update(desired_images(state))
    for values in ledger.get('pending', {}).values():
        result.update(values)
    return result


def available_identities(requested):
    """Resolve only locally present aliases; pending missing pulls remain names."""
    images, _ = docker_inventory()
    aliases = {}
    identities = sorted(images)
    for offset in range(0, len(identities), 100):
        rows = json.loads(command(['docker', 'image', 'inspect', *identities[offset:offset+100]]))
        for row in rows:
            for alias in [row['Id'], *(row.get('RepoTags') or []), *(row.get('RepoDigests') or [])]:
                aliases[alias] = row['Id']
    return {aliases.get(reference, reference) for reference in requested}


def reset_eligibility(protected=None):
    """Called while image_lock is held, including every managed renewed use."""
    try:
        with open(HISTORY_PATH) as source:
            history = json.load(source)
    except FileNotFoundError:
        return
    if protected is None:
        history['images'] = {}  # an observation gap cannot establish continuity
    else:
        for image in protected:
            history.get('images', {}).pop(image, None)
    atomic_json(HISTORY_PATH, history)


def remember_desired(state, previous=None):
    """Protect incoming intent before writing it or beginning any image pull."""
    with image_lock():
        ledger = read_references()
        current = desired_images(state)
        old = desired_images(previous or {})
        # Keep the immediately previous desired release as an immutable rollback
        # reference wherever it is already present. Missing images stay named.
        if current != set(ledger.get('current', [])):
            ledger['rollback'] = sorted(available_identities(old))
        ledger['current'] = sorted(current)
        reset_eligibility(available_identities(current | old))
        atomic_json(REFERENCES_PATH, ledger)


@contextlib.contextmanager
def protect_images(values):
    """Lifetime reference lease; no image lock is held during slow Docker work."""
    import uuid
    identity = uuid.uuid4().hex
    values = set(values)
    with image_lock():
        ledger = read_references()
        ledger.setdefault('pending', {})[identity] = sorted(values)
        reset_eligibility(available_identities(values))
        atomic_json(REFERENCES_PATH, ledger)
    try:
        yield
    finally:
        with image_lock():
            # Resolve again: a tag can now name the newly downloaded identity.
            reset_eligibility(available_identities(values))
            ledger = read_references()
            ledger.get('pending', {}).pop(identity, None)
            atomic_json(REFERENCES_PATH, ledger)


def run_worker():
    """Observe Docker references continuously; maintain age-only retention hourly.

    Reconnection resets eligibility rather than assuming that Docker's bounded
    event buffer covers every reference during a process or machine outage.
    This is deliberately conservative about redownloading cached images.
    """
    import subprocess
    import threading
    from reefy.storage_quota import Registry

    connected = threading.Event()

    def events():
        while True:
            process = None
            try:
                with image_lock():
                    reset_eligibility()
                process = subprocess.Popen(
                    ['docker', 'events', '--filter', 'type=container',
                     '--filter', 'event=create', '--format', '{{json .}}'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                connected.set()
                for line in process.stdout:
                    event = json.loads(line)
                    reference = ((event.get('Actor') or {}).get('Attributes') or {}).get('image')
                    with image_lock():
                        if reference:
                            reset_eligibility(available_identities({reference}))
                        else:
                            reset_eligibility()
            except Exception as error:
                print(f'[storage-images] event observation unavailable: {type(error).__name__}', flush=True)
            finally:
                connected.clear()
                if process is not None:
                    process.kill()
                    process.wait(timeout=5)
                with image_lock():
                    reset_eligibility()
            time.sleep(5)

    threading.Thread(target=events, daemon=True).start()
    while True:
        try:
            if Registry().data.get('active') and connected.is_set():
                removed = maintain(references=references)
                if removed:
                    print(f'[storage-images] removed {len(removed)} expired image(s)', flush=True)
        except Exception as error:
            print(f'[storage-images] maintenance postponed: {type(error).__name__}', flush=True)
        time.sleep(3600)


if __name__ == '__main__':
    run_worker()
