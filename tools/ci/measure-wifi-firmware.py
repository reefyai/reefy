#!/usr/bin/env python3
"""Measure a firmware-only change using the pinned package's native installer.

Run on Linux with an existing Buildroot checkout, verified firmware archive,
and mksquashfs. All build/install outputs stay in a new scratch directory.
No kernel build, network access, or device access is performed by this tool.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import time


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(args, log):
    with log.open('a') as stream:
        subprocess.run(args, check=True, stdout=stream, stderr=subprocess.STDOUT)


def inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            if not path.exists():
                raise RuntimeError(f'Broken firmware link: {relative}')
            if not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError(f'Escaping firmware link: {relative}')
            result[relative] = {'link': str(path.readlink())}
        elif path.is_file():
            result[relative] = {'bytes': path.stat().st_size, 'sha256': sha256(path)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--buildroot', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--baseline-defconfig', type=Path, required=True)
    parser.add_argument('--mksquashfs', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    scratch = args.scratch.resolve()
    scratch.mkdir(parents=True, exist_ok=False)
    log = scratch / 'measurement.log'
    recipe = args.buildroot / 'package/linux-firmware/linux-firmware.mk'
    hash_file = recipe.with_suffix('.hash')
    version = re.search(r'^LINUX_FIRMWARE_VERSION = (.+)$', recipe.read_text(), re.M)[1]
    expected = re.search(
        rf'^sha256\s+(\w+)\s+linux-firmware-{re.escape(version)}\.tar\.xz$',
        hash_file.read_text(), re.M)[1]
    archive_hash = sha256(args.archive)
    if archive_hash != expected:
        raise RuntimeError('Firmware archive does not match the pinned Buildroot hash')
    print('Verified upstream firmware archive', flush=True)
    with tarfile.open(args.archive) as archive:
        archive.extractall(scratch, filter='data')
    firmware = scratch / f'linux-firmware-{version}'
    before = args.baseline_defconfig.read_text()
    after = (source / 'configs/reefy_defconfig').read_text()
    added = sorted(set(re.findall(r'^(BR2_PACKAGE_LINUX_FIRMWARE_\w+)=y$', after, re.M))
                   - set(re.findall(r'^(BR2_PACKAGE_LINUX_FIRMWARE_\w+)=y$', before, re.M)))
    if not added:
        raise RuntimeError('No additional firmware selections to measure')

    reports = {}
    groups = {
        'baseline': [],
        'intel': [item for item in added if '_IWLWIFI_' in item],
        'atheros': [item for item in added if '_ATHEROS_' in item or '_QUALCOMM_' in item],
        'broadcom': [item for item in added if '_BRCM_' in item],
        'mediatek_ralink': [item for item in added if '_MEDIATEK_' in item or '_RALINK_' in item],
        'combined': added,
    }
    if set(added) != {item for name, items in groups.items()
                          if name != 'combined' for item in items}:
        raise RuntimeError('An added firmware selection has no measurement group')

    harness = scratch / 'install.mk'
    # Use the real package's build/install macros, including its WHENCE links.
    # XE firmware is removed from the OS by post_build.sh and is unchanged here.
    harness.write_text('''SHELL := /bin/bash
TAR := tar
qstrip = $(strip $(subst ",,$(1)))
define sep


endef
include $(CONFIG)
BR2_PACKAGE_LINUX_FIRMWARE_XE :=
include $(BUILDROOT)/package/linux-firmware/linux-firmware.mk
ifneq ($(WITH_OVERRIDE),)
include $(SOURCE)/board/reefy/reefy/package-overrides/linux-firmware.mk
endif
.PHONY: install
install: $(FIRMWARE)/.measurement-install
.PHONY: $(FIRMWARE)/.measurement-install
$(FIRMWARE)/.measurement-install:
\t$(file >$(MANIFEST),$(sort $(LINUX_FIRMWARE_FILES) $(LINUX_FIRMWARE_DIRS)))
\t$(file >$(LICENSES),$(LINUX_FIRMWARE_LICENSE_FILES))
\t$(LINUX_FIRMWARE_BUILD_CMDS)
\t$(foreach hook,$(LINUX_FIRMWARE_POST_BUILD_HOOKS),$(call $(hook))$(sep))
\t$(foreach hook,$(LINUX_FIRMWARE_PRE_INSTALL_TARGET_HOOKS),$(call $(hook))$(sep))
\t$(call LINUX_FIRMWARE_INSTALL_FW,$(DESTINATION)/lib/firmware)
\t$(foreach hook,$(LINUX_FIRMWARE_POST_INSTALL_TARGET_HOOKS),$(call $(hook))$(sep))
\t$(foreach hook,$(LINUX_FIRMWARE_PRE_INSTALL_IMAGES_HOOKS),$(call $(hook))$(sep))
\t$(call LINUX_FIRMWARE_INSTALL_FW,$(BINARIES_DIR))
\t$(foreach hook,$(LINUX_FIRMWARE_POST_INSTALL_IMAGES_HOOKS),$(call $(hook))$(sep))
''')
    for name, items in groups.items():
        group = scratch / name
        group.mkdir()
        config_input = group / 'defconfig'
        config_input.write_text(before + '\n' + ''.join(f'{item}=y\n' for item in items))
        config_output = group / 'config'
        run(['make', '-C', str(args.buildroot), f'O={config_output}',
             f'BR2_EXTERNAL={source}', f'BR2_DEFCONFIG={config_input}', 'defconfig'], log)
        config = config_output / '.config'
        actual = config.read_text()
        for item in items:
            if f'{item}=y\n' not in actual:
                raise RuntimeError(f'Kconfig dropped requested firmware: {item}')
        target = group / 'target'
        started = time.monotonic()
        run(['make', '-f', str(harness), 'install', f'CONFIG={config}',
             f'BUILDROOT={args.buildroot}', f'SOURCE={source}', f'FIRMWARE={firmware}',
             f'DESTINATION={target}', f'MANIFEST={group / "manifest.txt"}',
             f'LICENSES={group / "licenses.txt"}',
             f'HOST_DIR={args.mksquashfs.parent.parent}', f'BASE_DIR={group}',
             f'TARGET_DIR={target}', f'BINARIES_DIR={group / "images"}',
             'WITH_OVERRIDE=1'], log)
        for relative in (group / 'licenses.txt').read_text().split():
            if not (firmware / relative).is_file():
                raise RuntimeError(f'Missing package license: {relative}')
        entries = inventory(target)
        images = inventory(group / 'images')
        if images != {str(Path(name).relative_to('lib/firmware')): value
                      for name, value in entries.items()}:
            raise RuntimeError('Native target and image firmware installations differ')
        image = group / 'firmware.squashfs'
        run([str(args.mksquashfs), str(target), str(image), '-noappend',
             '-comp', 'xz', '-b', '128K', '-processors', '4', '-all-root',
             '-mkfs-time', '0', '-all-time', '0', '-no-progress'], log)
        reports[name] = {
            'regular_file_bytes': sum(item.get('bytes', 0) for item in entries.values()),
            'regular_files': sum('bytes' in item for item in entries.values()),
            'symlinks': sum('link' in item for item in entries.values()),
            'squashfs_bytes': image.stat().st_size,
            'seconds': round(time.monotonic() - started, 3),
            'inventory': entries,
        }
        print(f'{name}: {reports[name]["regular_file_bytes"]} unpacked bytes; '
              f'{reports[name]["squashfs_bytes"]} compressed bytes', flush=True)

    baseline = reports['baseline']
    for name, report in reports.items():
        report['added_regular_file_bytes'] = report['regular_file_bytes'] - baseline['regular_file_bytes']
        report['added_squashfs_bytes'] = report['squashfs_bytes'] - baseline['squashfs_bytes']
        if name != 'baseline':
            for path, original in baseline['inventory'].items():
                if report['inventory'].get(path) != original:
                    raise RuntimeError(f'Existing firmware changed or disappeared: {path}')

    combined = scratch / 'combined/target/lib/firmware'
    for relative in ['mediatek/mt7610u.bin', 'mediatek/mt7650e.bin',
                     'mediatek/mt7663pr2h.bin', 'mediatek/mt7663_n9_v3.bin',
                     'mediatek/mt7663pr2h_rebb.bin', 'mediatek/mt7663_n9_rebb.bin',
                     'ath9k_htc/htc_9271-1.4.0.fw',
                     'ath11k/WCN6855/hw2.0/amss.bin']:
        if not (combined / relative).is_file():
            raise RuntimeError(f'Required adapter firmware missing: {relative}')
    report = {
        'firmware_version': version, 'archive_sha256': archive_hash,
        'buildroot_revision': subprocess.check_output(
            ['git', '-C', str(args.buildroot), 'rev-parse', 'HEAD'], text=True).strip(),
        'baseline_defconfig_sha256': sha256(args.baseline_defconfig),
        'updated_defconfig_sha256': sha256(source / 'configs/reefy_defconfig'),
        'package_override_sha256': sha256(source / 'board/reefy/reefy/package-overrides/linux-firmware.mk'),
        'method': 'Native Buildroot config/package install; firmware-only SquashFS XZ, 128 KiB blocks; not a full firmware build',
        'added_selections': added, 'groups': reports,
    }
    (scratch / 'comparison.json').write_text(json.dumps(report, indent=2) + '\n')
    print('Firmware installation and preservation checks passed', flush=True)


if __name__ == '__main__':
    main()
