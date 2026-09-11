#!/usr/bin/env python3
"""Run kernel probes using the existing service-repository QEMU harness."""
import argparse
from pathlib import Path
import re
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--service-repo', type=Path, required=True)
    parser.add_argument('--firmware', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.service_repo / 'tests/e2e'))
    from lib.qemu_device import QemuDevice
    args.output.mkdir(parents=True, exist_ok=True)
    with QemuDevice(raw_image=args.firmware, log_path=args.output / 'qemu.log') as vm:
        vm.wait_for_boot(timeout_s=240)
        vm.scp_to(Path(__file__).with_name('setup_storage_probe.py'), '/tmp/setup_storage_probe.py')
        _, output, _ = vm.ssh_exec('python3 /tmp/setup_storage_probe.py', timeout_s=300)
        print(output)
        vm.scp_to(Path(__file__).with_name('kernel_storage_probe.py'), '/tmp/kernel_storage_probe.py')
        try:
            _, output, _ = vm.ssh_exec('python3 /tmp/kernel_storage_probe.py', timeout_s=300)
            print(output)
            (args.output / 'kernel-results.json').write_text(output)
            vm.scp_to(Path(__file__).with_name('controller_storage_probe.py'), '/tmp/controller_storage_probe.py')
            _, output, _ = vm.ssh_exec('python3 /tmp/controller_storage_probe.py', timeout_s=600)
            print(output)
            (args.output / 'controller-results.json').write_text(output)
            vm.scp_to(args.service_repo / 'tests/e2e/lib/phases/backup_quota_guest.py',
                      '/tmp/backup_quota_guest.py')
            _, output, _ = vm.ssh_exec('REEFY_E2E_QUOTA_GUEST=1 python3 /tmp/backup_quota_guest.py', timeout_s=600)
            print(output)
            (args.output / 'backup-results.log').write_text(output)
        finally:
            _, kernel, _ = vm.ssh_exec('dmesg', timeout_s=10)
            kernel = re.sub(r'password=\S+', 'password=[redacted]', kernel)
            (args.output / 'dmesg.log').write_text(kernel)
        assert 'Filesystem has been shut down' not in kernel
        assert 'out_of_data_space' not in kernel


if __name__ == '__main__':
    main()
