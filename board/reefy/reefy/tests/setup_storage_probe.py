#!/usr/bin/env python3
"""Provision the disposable test VM through Reefy's production storage path."""
import sys
sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage import Storage
from reefy.storage_quota import command, physical_sample, require_enforcement

command(['systemctl', 'stop', 'reefy-reconciler.service', 'docker.service', 'docker.socket'])
Storage()._ensure_persistent_storage(_log=print)
require_enforcement('/mnt/reefy-data')
assert physical_sample().healthy
command(['systemctl', 'start', 'docker.service'], timeout=120)
print('Disposable QEMU LUKS/thin/XFS storage provisioned')
