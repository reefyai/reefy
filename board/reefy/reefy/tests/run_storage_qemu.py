#!/usr/bin/env python3
"""Run kernel probes using the existing service-repository QEMU harness."""
import argparse
from pathlib import Path
import re
import json
import subprocess
import tempfile
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--service-repo', type=Path, required=True)
    parser.add_argument('--firmware', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--suite', choices=('all', 'core', 'core-nvme', 'thin', 'frigate', 'frigate-multi', 'migration'), default='all')
    args = parser.parse_args()
    sys.path.insert(0, str(args.service_repo / 'tests/e2e'))
    from lib.qemu_device import QemuDevice, QemuBlockDisk
    args.output.mkdir(parents=True, exist_ok=True)
    with QemuDevice(raw_image=args.firmware, log_path=args.output / 'qemu.log',
                    boot_disk_type='nvme' if args.suite == 'core-nvme' else 'virtio',
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
                (args.output / (result + '.error.log')).write_text(str(error))
                failures.append(source.name)
                if isinstance(error, TimeoutError):
                    # Closing SSH cannot prove the timed-out writer exited.
                    # Tear down this disposable VM before another mutation.
                    raise
                # Independent of Docker, which may be frozen or unresponsive.
                _, evidence, _ = vm.ssh_exec(
                    'journalctl -b -u reefy-storage-guard -u reefy-storage-watchdog '
                    '-u reefy-storage-hold --no-pager -n 100; '
                    'sudo cat /run/reefy/storage-pressure/hold.json; '
                    'sudo cat /run/reefy/storage-pressure/status.json; '
                    'sudo cat /run/reefy/storage-pressure/session.json; '
                    'sudo cat /sys/fs/cgroup/system.slice/docker.service/cgroup.events',
                    timeout_s=20, check=False)
                (args.output / (source.stem + '-protection.log')).write_text(evidence)
                return False
            finally:
                _, kernel_log, _ = vm.ssh_exec('dmesg', timeout_s=10)
                kernel_log = re.sub(r'password=\S+', 'password=[redacted]', kernel_log)
                (args.output / (source.stem + '-dmesg.log')).write_text(kernel_log)
                if any(value in kernel_log for value in ('Filesystem has been shut down', 'out_of_data_space')):
                    failures.append(source.name + ': kernel storage failure')

        try:
            if args.suite in ('all', 'core', 'core-nvme', 'thin'):
                probe(Path(__file__).with_name('kernel_storage_probe.py'),
                      'python3 /tmp/kernel_storage_probe.py', 'kernel-results.json', 300)
            if args.suite in ('all', 'thin'):
                thin_ready = probe(Path(__file__).with_name('thin_storage_probe.py'),
                                   'python3 /tmp/thin_storage_probe.py', 'thin-results.json', 600)
                if thin_ready:
                    probe(Path(__file__).with_name('pressure_storage_probe.py'),
                          'python3 /tmp/pressure_storage_probe.py', 'pressure-results.json', 660)
                    _, trace, _ = vm.ssh_exec('cat /tmp/synthetic-pressure-trace.json', timeout_s=20, check=False)
                    (args.output / 'pressure-trace.json').write_text(trace)
                    cow_ready = probe(Path(__file__).with_name('cow_storage_probe.py'),
                                      'python3 /tmp/cow_storage_probe.py', 'cow-results.json', 180)
                    _, trace, _ = vm.ssh_exec('cat /tmp/synthetic-cow-trace.json', timeout_s=20, check=False)
                    (args.output / 'cow-trace.json').write_text(trace)
                    if cow_ready:
                        probe(Path(__file__).with_name('amplification_storage_probe.py'),
                              'python3 /tmp/amplification_storage_probe.py', 'amplification-results.json', 120)
                        _, trace, _ = vm.ssh_exec('cat /tmp/synthetic-sparse-trace.json', timeout_s=20, check=False)
                        (args.output / 'amplification-trace.json').write_text(trace)
            ready = False
            if args.suite != 'thin':
                ready = probe(Path(__file__).with_name('controller_storage_probe.py'),
                              'python3 /tmp/controller_storage_probe.py', 'controller-results.json', 600)
            if ready:
                recovered = True
                if args.suite in ('all', 'core', 'core-nvme'):
                    # Build only a synthetic legacy rootfs on this Linux runner;
                    # no real old firmware or customer configuration is needed.
                    from test_storage_firmware import firmware_fixture
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory) / 'root'
                        manifest = root / 'usr/share/reefy/compatibility.json'
                        manifest.parent.mkdir(parents=True)
                        manifest.write_text(json.dumps({'manifest_version': 1, 'protocols': {
                            'desired_state': {'versions': [1], 'features': {}}}}))
                        squash = Path(directory) / 'rootfs.squashfs'
                        subprocess.run(['mksquashfs', str(root), str(squash), '-noappend',
                                        '-processors', '1', '-quiet'], check=True)
                        target = Path(directory) / 'synthetic-legacy.efi'
                        target.write_bytes(firmware_fixture(squash.read_bytes())[0])
                        vm.scp_to(target, '/tmp/synthetic-legacy.efi')
                    probe(Path(__file__).with_name('firmware_storage_probe.py'),
                          'python3 /tmp/firmware_storage_probe.py', 'firmware-results.json', 120)
                    probe(Path(__file__).with_name('runtime_storage_probe.py'),
                          'python3 /tmp/runtime_storage_probe.py', 'runtime-results.json', 240)
                    probe(Path(__file__).with_name('image_retention_probe.py'),
                          'python3 /tmp/image_retention_probe.py', 'retention-results.json', 180)
                    probe(args.service_repo / 'tests/e2e/lib/phases/backup_quota_guest.py',
                          'REEFY_E2E_QUOTA_GUEST=1 python3 /tmp/backup_quota_guest.py', 'backup-results.log', 600)
                    recovered = probe(Path(__file__).with_name('storage_failure_probe.py'),
                                      'python3 /tmp/storage_failure_probe.py', 'failure-results.json', 180)
                if args.suite in ('all', 'frigate'):
                    frigate_ready = probe(Path(__file__).with_name('frigate_storage_probe.py'),
                          'python3 /tmp/frigate_storage_probe.py', 'frigate-results.json', 1500)
                    if frigate_ready:
                        probe(Path(__file__).with_name('frigate_exports_probe.py'),
                              'python3 /tmp/frigate_exports_probe.py', 'exports-results.json', 180)
                        probe(Path(__file__).with_name('sqlite_full_probe.py'),
                              'python3 /tmp/sqlite_full_probe.py', 'sqlite-results.json', 180)
                if args.suite in ('all', 'frigate-multi'):
                    vm.scp_to(Path(__file__).with_name('frigate_storage_probe.py'), '/tmp/frigate_storage_probe.py')
                    probe(Path(__file__).with_name('frigate_multi_probe.py'),
                          'python3 /tmp/frigate_multi_probe.py', 'frigate-multi-results.json', 1500)
                    for name in ('low', 'high'):
                        _, evidence, _ = vm.ssh_exec('cat /tmp/synthetic-frigate-' + name + '.log',
                                                     timeout_s=20, check=False)
                        (args.output / ('frigate-' + name + '-worker.log')).write_text(evidence)
                if recovered and args.suite in ('all', 'migration'):
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
