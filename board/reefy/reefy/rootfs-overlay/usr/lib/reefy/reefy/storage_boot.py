"""Bound boot storage recovery and retain diagnostics instead of retry loops.

The ESP is already mounted; this supervisor never depends on the data volume.
Three hours is an availability policy, not a filesystem completion estimate.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ESP = Path('/mnt/reefy')
RUN = Path('/run/reefy')
MARKER = ESP / 'reefy/recovery/storage-timeout.json'
PARTIAL = MARKER.with_suffix('.partial')
DEADLINE = 3 * 60 * 60
# These bound interruption/control operations, never normal repair work.
INTERRUPT_GRACE = 60
CONTROL_GRACE = 30
MAX_REPORT = 64 * 1024


def log(message):
    print('[storage-backstop] ' + message, flush=True)


def call(args, timeout=CONTROL_GRACE, check=True):
    # Avoid subprocess.run's unbounded wait after killing a task in disk sleep.
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, start_new_session=True)
    except OSError as error:
        raise RuntimeError(f'cannot start {args[0]}: {error}') from error
    try:
        out, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise RuntimeError('control operation timed out: ' + args[0])
    if check and process.returncode:
        raise RuntimeError(f'{args[0]} exited {process.returncode}: {out[-2048:]}')
    return process.returncode, out


def fail_guard():
    RUN.mkdir(parents=True, exist_ok=True)
    (RUN / 'storage-recovery-failed').write_text(
        'Storage recovery timed out; repair storage before provisioning\n')
    (RUN / 'storage-recovery-timeout').touch()


def group_members(pgid):
    """Ignore zombies, but include descendants after the original shell exits."""
    members = []
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            text = path.read_text()
            fields = text[text.rindex(')') + 2:].split()
            if int(fields[2]) == pgid and fields[0] not in ('Z', 'X'):
                members.append(int(path.parent.name))
        except (OSError, ValueError, IndexError):
            continue
    return members


def interrupt(process, grace=INTERRUPT_GRACE):
    # SIGINT first matches the documented stuck-xfs_repair interruption path.
    # Do not treat termination as a successful repair or mount afterward.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        until = time.monotonic() + grace
        while time.monotonic() < until:
            process.poll()  # Reap the shell before inspecting descendants.
            if not group_members(process.pid):
                return True
            time.sleep(0.25)
    return False


def report(elapsed, pgid):
    data = {'reason': 'storage recovery deadline exceeded',
            'elapsed_seconds': int(elapsed), 'processes': []}
    for pid in group_members(pgid)[:8]:
        entry = {'pid': pid}
        for name in ('comm', 'wchan', 'status', 'io'):
            try:
                entry[name] = (Path('/proc') / str(pid) / name).read_text()[:1024]
            except OSError as error:
                entry[name] = str(error)
        data['processes'].append(entry)
    # Limit input/output as well as the persisted report. Journal order gives
    # the last recovery phase; do not scrape command lines containing secrets.
    try:
        _, recent = call(['journalctl', '-b', '-u', 'reefy-storage.service',
                          '-n', '80', '--no-pager', '--output=short-monotonic'], timeout=5)
        data['recent_storage_log'] = recent[-16000:]
    except RuntimeError as error:
        data['journal_error'] = str(error)
    payload = json.dumps(data, indent=2).encode()
    while len(payload) > MAX_REPORT:
        if data.get('recent_storage_log'):
            data['recent_storage_log'] = data['recent_storage_log'][len(data['recent_storage_log']) // 2:]
        elif data['processes']:
            data['processes'].pop()
        else:
            raise RuntimeError('cannot bound timeout report')
        payload = json.dumps(data, indent=2).encode()
    return payload


def persist(source):
    """Worker: durable marker on the already-mounted ESP, with bounded bytes."""
    from reefy.vg_recovery import writable_esp
    payload = source.read_bytes()
    if len(payload) > MAX_REPORT:
        raise RuntimeError('timeout report exceeds limit')
    with writable_esp():
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        # Preserve first evidence; an incomplete write also inhibits recovery.
        if MARKER.exists() or PARTIAL.exists():
            raise RuntimeError('prior timeout evidence exists')
        with PARTIAL.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(PARTIAL, MARKER)
        fd = os.open(MARKER.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    # Another recovery worker may already have made the ESP writable.
    # Require it read-only before authorizing a reset in that case too.
    call(['mount', '-o', 'remount,ro', str(ESP)])
    if MARKER.read_bytes() != payload:
        raise RuntimeError('timeout marker verification failed')


def preserve(payload):
    RUN.mkdir(parents=True, exist_ok=True)
    source = RUN / 'storage-timeout.json'
    source.write_bytes(payload)
    try:
        call([sys.executable, '-m', 'reefy.storage_boot', '--persist', str(source)])
        return True
    except RuntimeError as error:
        log(f'Cannot preserve reboot guard: {error}; automatic reboot forbidden')
        return False


def detach_data():
    # Services have not started, but recovery might have mounted a filesystem
    # before stalling. Never release consumers onto that unverified mount.
    try:
        code, mounts = call(['findmnt', '-rn', '-o', 'TARGET'], check=False)
        if code:
            return False
        targets = [p for p in mounts.splitlines()
                   if p == '/mnt/reefy-data' or p.startswith('/mnt/reefy-data/')]
        for target in sorted(targets, key=len, reverse=True):
            call(['umount', target])  # No lazy or forced unmount.
        return True
    except RuntimeError as error:
        log(f'Data mounts could not be detached: {error}')
        return False


def reboot_current():
    # A/B rollback might boot firmware too old to understand the guard. Retry
    # the current firmware once, without confirming it or changing BootOrder.
    _, label = call(['findmnt', '-rn', '-o', 'LABEL', str(ESP)])
    if label.strip() not in ('reefy-a', 'reefy-b'):
        raise RuntimeError('cannot identify current firmware for diagnostic reboot')
    # This existing helper validates partition GUIDs, opens efivarfs for
    # writing, sets and verifies BootNext, then restores efivarfs read-only.
    # BootCurrent itself may name an entry removed by early boot cleanup.
    call(['reefy-efi', 'set-next', label.strip()[-1]])
    # Recovery resisted both signals or its mount could not be detached.
    # A normal shutdown can hang on that same I/O. The ESP marker is durable
    # and remounted read-only; reset without depending on the stalled disk.
    log('Last resort: rebooting current firmware into guarded diagnostics')
    Path('/proc/sysrq-trigger').write_text('b')


def supervise(command, deadline=DEADLINE):
    if MARKER.exists() or PARTIAL.exists():
        fail_guard()
        log('Persistent timeout guard present; diagnostics only, storage untouched')
        return 1
    started = time.monotonic()
    process = subprocess.Popen(command, start_new_session=True)
    try:
        return process.wait(timeout=deadline)
    except subprocess.TimeoutExpired:
        fail_guard()  # Also inhibits the A/B watchdog immediately.
        log(f'Storage recovery exceeded {deadline}s; preserving evidence and interrupting')
        durable = preserve(report(time.monotonic() - started, process.pid))
        stopped = interrupt(process)
        if stopped and detach_data():
            log('Recovery stopped; releasing boot into bootstrap diagnostics')
            return 1
        if durable:
            try:
                reboot_current()
                log('Recovery could not stop; requested reboot into guarded diagnostics')
            except (RuntimeError, OSError) as error:
                log(f'Automatic reboot refused: {error}')
        # A surviving writer or mount is unsafe for consumers. Keep the unit
        # activating if reboot cannot happen. Console remains the fallback.
        while True:
            time.sleep(60)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--persist':
        persist(Path(sys.argv[2]))
        return 0
    return supervise(['/usr/bin/boot-reefy-storage.sh', '--recover'])


if __name__ == '__main__':
    sys.exit(main())
