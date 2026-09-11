#!/usr/bin/env python3
"""Exercise target-image admission without writing either QEMU boot slot."""
import json
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
    require_compatible_image(current)
    before = command(['efibootmgr', '-v'])
    result = subprocess.run(['reefy-efi', 'update', '/tmp/synthetic-legacy.efi'],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0, result.stdout
    assert 'does not support the active storage quota policy' in result.stderr, result.stderr
    assert 'Formatting' not in result.stdout, result.stdout
    assert command(['efibootmgr', '-v']) == before
    assert not list(Path('/run').glob('reefy-firmware-check-*'))
    assert not Path('/run/reefy/storage-pressure/hold.json').exists()
    assert command(['systemctl', 'is-active', 'docker.service']).strip() == 'active'
    print(json.dumps({'actual_uki_capability_verified_and_legacy_update_rejected': 'passed'}))


if __name__ == '__main__':
    run()
