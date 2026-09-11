#!/usr/bin/env python3
"""Exercise target-image admission without writing either QEMU boot slot."""
import json
import shutil
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_firmware import require_compatible_image
from reefy.storage_quota import Registry, command


def run():
    assert Registry().data.get('active')
    # Verify the actual booted UKI through PE/newc/SquashFS and its immutable
    # compatibility manifest, including loop-mount cleanup.
    current = Path('/mnt/reefy/EFI/Boot/bootx64.efi')
    assert current.is_file()
    loops_before = command(['losetup', '-a'])
    require_compatible_image(current)
    assert command(['losetup', '-a']) == loops_before
    before = command(['efibootmgr', '-v'])
    result = subprocess.run(['reefy-efi', 'update', '/tmp/synthetic-legacy.efi'],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0, result.stdout
    assert 'does not support the active storage quota policy' in result.stderr, result.stderr
    assert 'Formatting' not in result.stdout, result.stdout
    assert command(['efibootmgr', '-v']) == before
    # This VM boots the synthetic raw image's A slot. Exercise the operator's
    # slot-selection path too, using only its disposable inactive B partition.
    source = json.loads(command(['findmnt', '--json', '--target', '/mnt/reefy',
                                 '-o', 'SOURCE']))['filesystems'][0]['source']
    assert command(['blkid', '-s', 'PARTLABEL', '-o', 'value', source]).strip() == 'reefy-a'
    inactive = command(['blkid', '-t', 'PARTLABEL=reefy-b', '-o', 'device']).splitlines()
    assert len(inactive) == 1
    mount = Path('/run/synthetic-inactive-firmware')
    mount.mkdir()
    command(['mount', '-t', 'vfat', inactive[0], str(mount)])
    try:
        destination = mount / 'EFI/Boot/bootx64.efi'
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile('/tmp/synthetic-legacy.efi', destination)
        command(['sync'])
    finally:
        command(['umount', str(mount)])
        mount.rmdir()
    result = subprocess.run(['reefy-efi', 'set-next', 'b'],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert 'does not support the active storage quota policy' in result.stderr, result.stderr
    assert command(['efibootmgr', '-v']) == before
    assert command(['losetup', '-a']) == loops_before
    assert not list(Path('/run').glob('reefy-firmware-check-*'))
    assert not Path('/run/reefy/storage-pressure/hold.json').exists()
    assert command(['systemctl', 'is-active', 'docker.service']).strip() == 'active'
    print(json.dumps({'actual_uki_capability_verified_and_legacy_update_rejected': 'passed',
                      'legacy_slot_selection_rejected': 'passed'}))


if __name__ == '__main__':
    run()
