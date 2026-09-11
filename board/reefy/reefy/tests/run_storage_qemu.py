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
    from lib.qemu_device import QemuDevice, QemuBlockDisk
    args.output.mkdir(parents=True, exist_ok=True)
    with QemuDevice(raw_image=args.firmware, log_path=args.output / 'qemu.log',
                    extra_block_disks=(QemuBlockDisk(size='16G', serial='quota-e2e-pool'),)) as vm:
        vm.wait_for_boot(timeout_s=240)
        vm.scp_to(Path(__file__).with_name('setup_storage_probe.py'), '/tmp/setup_storage_probe.py')
        _, output, _ = vm.ssh_exec('python3 /tmp/setup_storage_probe.py', timeout_s=300)
        print(output)
        failures = []

        def probe(source, command, result, timeout):
            vm.scp_to(source, '/tmp/' + source.name)
            try:
                _, output, _ = vm.ssh_exec(command, timeout_s=timeout)
                print(output)
                (args.output / result).write_text(output)
                return True
            except Exception as error:
                print(f'{source.name} FAILED: {error}', flush=True)
                failures.append(source.name)
                return False

        try:
            probe(Path(__file__).with_name('kernel_storage_probe.py'),
                  'python3 /tmp/kernel_storage_probe.py', 'kernel-results.json', 300)
            probe(Path(__file__).with_name('thin_storage_probe.py'),
                  'python3 /tmp/thin_storage_probe.py', 'thin-results.json', 600)
            ready = probe(Path(__file__).with_name('controller_storage_probe.py'),
                          'python3 /tmp/controller_storage_probe.py', 'controller-results.json', 600)
            if ready:
                probe(Path(__file__).with_name('image_retention_probe.py'),
                      'python3 /tmp/image_retention_probe.py', 'retention-results.json', 180)
                probe(args.service_repo / 'tests/e2e/lib/phases/backup_quota_guest.py',
                      'REEFY_E2E_QUOTA_GUEST=1 python3 /tmp/backup_quota_guest.py', 'backup-results.log', 600)
                probe(Path(__file__).with_name('frigate_storage_probe.py'),
                      'python3 /tmp/frigate_storage_probe.py', 'frigate-results.json', 1500)
                recovered = probe(Path(__file__).with_name('storage_failure_probe.py'),
                                  'python3 /tmp/storage_failure_probe.py', 'failure-results.json', 180)
                if recovered:
                    from run_storage_boot import run_boot_migration
                    try:
                        run_boot_migration(vm, args.output)
                    except Exception as error:
                        print(f'Boot migration FAILED: {error}', flush=True)
                        failures.append('boot migration')
        finally:
            _, kernel, _ = vm.ssh_exec('dmesg', timeout_s=10)
            kernel = re.sub(r'password=\S+', 'password=[redacted]', kernel)
            (args.output / 'dmesg.log').write_text(kernel)
        assert not failures, failures
        assert 'Filesystem has been shut down' not in kernel
        assert 'out_of_data_space' not in kernel


if __name__ == '__main__':
    main()
