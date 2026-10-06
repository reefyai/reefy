"""Bounded recovery for known Borg SSH transport failures."""
import json
import subprocess
import time

ATTEMPTS = 3
FATAL = ('permission denied', 'invalid passphrase', 'wrong passphrase',
         'data integrity error', 'not a valid repository',
         'repository does not exist', 'quota exceeded', 'no space left')


def retryable(stderr):
    text = stderr.lower()
    return (not any(word in text for word in FATAL)
            and any(word in text for word in (
                'connection closed by remote host', 'connection reset by peer')))


def list_archive(archive, env, emit, timeout_s=120):
    if timeout_s <= 0:
        raise ValueError('Borg timeout must be positive')
    deadline = time.monotonic() + timeout_s
    result = None
    for attempt in range(1, ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if result is None:
                raise subprocess.TimeoutExpired(['borg', 'list'], timeout_s)
            return result
        result = subprocess.run(['borg', 'list', '--short', archive], env=env,
                                capture_output=True, text=True, timeout=remaining)
        delay = 2 * attempt
        if (result.returncode == 0 or not retryable(result.stderr or '')
                or attempt == ATTEMPTS
                or time.monotonic() + delay >= deadline):
            return result
        emit(f'Borg archive read transport failure; retrying ({attempt + 1}/{ATTEMPTS})')
        time.sleep(delay)
    return result


def extract_archive(command, env, directory, emit, timeout_s=6 * 3600):
    # Caller holds restore ownership and withholds the completion marker. Each
    # attempt restores the same immutable archive into that instance's volumes.
    # The caller writes .restored only after an entire attempt succeeds.
    if timeout_s <= 0:
        raise ValueError('Borg timeout must be positive')
    deadline = time.monotonic() + timeout_s
    for attempt in range(1, ATTEMPTS + 1):
        process = subprocess.Popen(command, env=env, cwd=directory,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        last_progress = float('-inf')
        error_tail = ''
        fatal_seen = False
        for raw in iter(process.stderr.readline, b''):
            if time.monotonic() > deadline:
                process.kill()
                break
            line = raw.decode('utf-8', errors='replace').rstrip()
            if not line:
                continue
            fatal_seen |= any(word in line.lower() for word in FATAL)
            error_tail = (error_tail + '\n' + line)[-8192:]
            try:
                row = json.loads(line)
            except ValueError:
                emit('borg: ' + line)
                continue
            if row.get('type') == 'progress_percent' and not row.get('finished'):
                now = time.monotonic()
                if now - last_progress < 5:
                    continue
                last_progress = now
            emit('borg: ' + line)
        process.wait()
        delay = 2 * attempt
        if (process.returncode == 0 or fatal_seen or not retryable(error_tail)
                or attempt == ATTEMPTS
                or time.monotonic() + delay >= deadline):
            return process.returncode
        emit(f'Borg extract transport failure; retrying ({attempt + 1}/{ATTEMPTS})')
        time.sleep(delay)
    return process.returncode
