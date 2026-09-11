#!/usr/bin/env python3
"""Run kernel probes using the existing service-repository QEMU harness."""
import argparse
from pathlib import Path
import shlex
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
        vm.scp_to(Path(__file__).with_name('kernel_storage_probe.py'), '/tmp/kernel_storage_probe.py')
        _, output, _ = vm.ssh_exec('python3 /tmp/kernel_storage_probe.py', timeout_s=300)
        print(output)
        (args.output / 'kernel-results.json').write_text(output)
        _, kernel, _ = vm.ssh_exec('dmesg', timeout_s=10)
        (args.output / 'dmesg.log').write_text(kernel)
        assert 'Filesystem has been shut down' not in kernel
        assert 'out_of_data_space' not in kernel


if __name__ == '__main__':
    main()
