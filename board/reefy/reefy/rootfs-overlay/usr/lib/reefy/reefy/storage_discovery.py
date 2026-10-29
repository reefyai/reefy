"""Wait for existing encrypted storage before the boot recovery path runs.

LVM's own metadata supplies VG membership. Discovery never formats storage,
activates a partial VG, runs repair, or writes configuration to the boot ESP.
"""
import json
from pathlib import Path
import subprocess
import sys
import time

DEADLINE = 15 * 60
GUARD = Path('/run/reefy/storage-recovery-failed')


def run(args, timeout=10):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def key_partition(boot_device):
    result = run(['lsblk', '-no', 'PKNAME', boot_device])
    parent = result.stdout.strip()
    if result.returncode or not parent:
        raise RuntimeError('Cannot identify boot disk for storage discovery')
    disk = parent if parent.startswith('/dev/') else '/dev/' + parent
    return disk, Path(disk + ('p3' if disk[-1].isdigit() else '3'))


def complete_vg(timeout):
    result = run(['pvs', '--reportformat', 'json', '-o', 'pv_name,pv_missing',
                  '--select', 'vg_name=reefy'], timeout=timeout)
    rows = json.loads(result.stdout)['report'][0]['pv']
    if rows:
        return result.returncode == 0 and all(not row['pv_missing'].strip() for row in rows)
    # Matching LUKS storage with no readable VG must reach the existing
    # guarded outer-metadata recovery path, once, rather than retry repairs.
    if result.returncode:
        raise RuntimeError('Cannot inspect existing Reefy VG membership')
    return True


def wait(boot_device, now=time.monotonic, sleep=time.sleep):
    disk, key = key_partition(boot_device)
    if not key.is_block_device():
        return  # Fresh image or wipe-only reset: adoption starts immediately.
    for module in ('dm_crypt', 'dm_mod'):
        run(['modprobe', module])
    until = now() + DEADLINE
    opened, rejected = set(), set()
    refresh_needed = False
    usb_data = disk + ('p4' if disk[-1].isdigit() else '4')
    print('[reefy] Waiting for existing storage and complete VG membership (up to 900s)', flush=True)

    def budget(maximum):
        remaining = min(maximum, until - now())
        if remaining <= 0:
            raise TimeoutError('Existing storage did not assemble within 900 seconds')
        return remaining

    def deferred_timeout():
        print('[reefy] Storage discovery command timed out; retrying within 900s policy', flush=True)

    while True:
        try:
            listing = run(['lsblk', '-rpn', '-o', 'NAME,TYPE'], timeout=budget(10))
            if listing.returncode:
                raise RuntimeError('Cannot enumerate storage devices')
            for line in listing.stdout.splitlines():
                if not line.strip():
                    continue
                path, kind = line.split()
                # Match existing boot behavior: whole internal drives and the
                # optional encrypted USB data partition. Other partitions stay alone.
                if path == disk or (kind != 'disk' and path != usb_data) or path in opened:
                    continue
                try:
                    probe = run(['cryptsetup', 'luksUUID', path], timeout=budget(10))
                except subprocess.TimeoutExpired:
                    # An unrelated slow disk must not hide an already complete
                    # owned VG. Retry this probe on a later discovery pass.
                    deferred_timeout()
                    continue
                if probe.returncode or not probe.stdout.strip():
                    continue
                signature = (path, probe.stdout.strip())
                if signature in rejected:
                    continue
                name = 'reefy-data' if path == usb_data else 'reefy-' + Path(path).name
                if not Path('/dev/mapper', name).exists():
                    result = run(['cryptsetup', 'luksOpen', path, name,
                                  '--allow-discards', '--perf-submit_from_crypt_cpus', '--persistent',
                                  '--key-file', str(key), '--keyfile-size', '44'], timeout=budget(120))
                    if result.returncode:
                        if result.returncode == 2:
                            rejected.add(signature)
                        else:
                            # Device-mapper/udev can finish creating the mapping
                            # even when cryptsetup reports a transient failure.
                            # Recheck on the next bounded discovery iteration.
                            print(f'[reefy] Encrypted disk open deferred (status {result.returncode})', flush=True)
                        continue
                opened.add(path)
                refresh_needed = True
            if opened:
                # Refresh discovery after each new mapping, before reading current
                # membership. Never use vgchange --partial to bypass missing disks.
                if refresh_needed:
                    run(['vgscan'], timeout=budget(10))
                    refresh_needed = False
                # Legacy USB p4 can contain a filesystem directly, without LVM.
                if usb_data in opened:
                    fs = run(['blkid', '-p', '-s', 'TYPE', '-o', 'value', '/dev/mapper/reefy-data'],
                             timeout=budget(5))
                    if fs.returncode == 0 and fs.stdout.strip() in ('f2fs', 'ext4', 'xfs'):
                        return
                if complete_vg(budget(10)):
                    print('[reefy] Existing storage discovery complete', flush=True)
                    return
        except subprocess.TimeoutExpired:
            # A bounded discovery command can stall while disks settle. It is
            # not evidence that the full membership deadline has elapsed.
            # Keep opened mappings and a pending VG refresh for the next pass;
            # no repair, partial activation or formatting occurs in this loop.
            deferred_timeout()
        sleep(min(0.25, budget(0.25)))


def verify_mount(boot_device):
    _, key = key_partition(boot_device)
    if not key.is_block_device():
        return
    result = run(['findmnt', '-rn', '-o', 'FSTYPE', '--mountpoint', '/mnt/reefy-data'])
    if result.returncode or result.stdout.strip() not in ('f2fs', 'ext4', 'xfs'):
        raise RuntimeError('Existing encrypted storage did not mount; provisioning refused')


def main():
    try:
        if len(sys.argv) == 2:
            wait(sys.argv[1])
        elif len(sys.argv) == 3 and sys.argv[1] == '--mounted':
            verify_mount(sys.argv[2])
        else:
            raise ValueError('Invalid internal storage-discovery operation')
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        GUARD.parent.mkdir(parents=True, exist_ok=True)
        GUARD.write_text('Existing storage unavailable; repair storage before provisioning\n')
        print(f'[reefy] Storage discovery refused: {error}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
