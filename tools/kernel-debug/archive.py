#!/usr/bin/env python3
"""Archive and verify exact kernel debug outputs, never the signing keys."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
import time
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def elf_info(path, readelf):
    notes = run(readelf, '-n', str(path))
    match = re.search(r'Build ID: ([0-9a-f]+)', notes)
    if not match:
        raise RuntimeError(f'missing GNU build ID: {path}')
    sections = run(readelf, '-SW', str(path))
    return match[1], bool(re.search(r'\s\.debug_info\s', sections)), bool(re.search(r'\s\.BTF\s', sections))


def require_debug(path, readelf, btf=True):
    identity, dwarf, has_btf = elf_info(path, readelf)
    if not dwarf or (btf and not has_btf):
        raise RuntimeError(f'missing required DWARF/BTF: {path}')
    return identity


def worker_count():
    # Bound disk/process pressure even on large build hosts.
    return min(16, os.cpu_count() or 1)


def parallel_map(function, items):
    with ThreadPoolExecutor(max_workers=worker_count()) as pool:
        return list(pool.map(function, items))


def timed(label, function):
    started = time.monotonic()
    result = function()
    print(f'[debug-archive] {label}: {time.monotonic() - started:.1f}s', flush=True)
    return result


def compress_bundle(stage, destination):
    # Keep the existing gzip archive contract and propagate tar/pigz failures.
    subprocess.run(['tar', '-I', f'pigz -p {worker_count()}', '-cf',
                    str(destination), '-C', str(stage), '.'], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    repo = Path.cwd()
    releases = list((output / 'target/lib/modules').iterdir())
    if len(releases) != 1:
        raise RuntimeError('expected exactly one installed kernel release')
    release = releases[0].name
    kernels = [p for p in (output / 'build').glob('linux-*')
               if (p / 'include/config/kernel.release').is_file()
               and (p / 'include/config/kernel.release').read_text().strip() == release]
    if len(kernels) != 1:
        raise RuntimeError('expected exactly one matching kernel tree')
    kernel = kernels[0]
    prefix = str(output / 'host/bin/x86_64-buildroot-linux-gnu-')
    readelf = prefix + 'readelf'
    kernel_id = require_debug(kernel / 'vmlinux', readelf)
    # Exercise the BTF parser, not just section-name presence.
    run(str(output / 'host/bin/pahole'), '-F', 'btf', '-C', 'task_struct', str(kernel / 'vmlinux'))
    config = (kernel / '.config').read_text()
    for option in ('CONFIG_DEBUG_INFO_BTF=y', 'CONFIG_DEBUG_INFO_BTF_MODULES=y'):
        if option not in config.splitlines():
            raise RuntimeError(f'resolved configuration missing {option}')
    destination = output / 'images/reefy-kernel-debug.tar.gz'
    with tempfile.TemporaryDirectory(prefix='reefy-debug-') as temporary:
        stage = Path(temporary) / 'bundle'
        stage.mkdir()

        def copy(source, name):
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        for name in ('vmlinux', 'System.map', '.config', 'Module.symvers',
                     'modules.order', 'modules.builtin', 'modules.builtin.modinfo'):
            copy(kernel / name, 'kernel/' + name)
        for directory in ('include/generated', 'arch/x86/include/generated'):
            shutil.copytree(kernel / directory, stage / 'kernel' / directory)
        copy(output / '.config', 'buildroot.config')
        copy(output / 'target/usr/lib/os-release', 'os-release')
        copy(repo / 'board/reefy/reefy/provider-publisher-pins', 'provider-publisher-pins')
        # Match both intermediate images and the kernel embedded in every UKI.
        if digest(kernel / 'arch/x86/boot/bzImage') != digest(output / 'images/bzImage'):
            raise RuntimeError('packaged bzImage differs from debug kernel build')
        for flavor in ('dev', 'prod', 'debug-shell'):
            extracted = Path(temporary) / 'uki-kernel'
            subprocess.run([prefix + 'objcopy', '--dump-section',
                            f'.linux={extracted}', str(output / f'images/reefy-{flavor}.efi'),
                            str(Path(temporary) / 'uki-copy')], check=True)
            if digest(extracted) != digest(kernel / 'arch/x86/boot/bzImage'):
                raise RuntimeError(f'{flavor} UKI kernel mismatch')
        extracted = Path(temporary) / 'vmlinux'
        with extracted.open('wb') as stream:
            subprocess.run(['bash', str(kernel / 'scripts/extract-vmlinux'),
                            str(output / 'images/bzImage')], stdout=stream, check=True)
        if elf_info(extracted, readelf)[0] != kernel_id:
            raise RuntimeError('boot kernel build ID mismatch')

        # Buildroot's installed copies may be stripped. Index originals by ID,
        # not basename, to avoid mixing in-tree and external provider modules.
        originals = {}
        def inspect_original(module):
            return module, elf_info(module, readelf)

        original_paths = sorted(p for p in (output / 'build').rglob('*.ko')
                                if not p.is_symlink())
        for module, (identity, dwarf, btf) in timed(
                'index originals', lambda: parallel_map(inspect_original, original_paths)):
            if dwarf:
                originals[identity] = module
        roots = [('base', output / 'target')]
        for provider in ('intel', 'nvidia', 'amd'):
            roots.append((provider, output / f'reefy-artifacts/{provider}/modules-root'))
        # AMD's pinned builder preserves debug sections in the staged signed copy.
        for _, root in roots:
            for module in root.rglob('*.ko'):
                identity, dwarf, _ = elf_info(module, readelf)
                if dwarf:
                    originals.setdefault(identity, module)
        tasks = []
        for label, root in roots:
            shipped_paths = sorted(p for p in (root / 'lib/modules' / release).rglob('*.ko*')
                                   if p.is_file())
            if not shipped_paths:
                raise RuntimeError(f'no shipped modules in {label}')
            tasks.extend((label, root, shipped) for shipped in shipped_paths)

        def verify_module(task):
            label, root, shipped = task
            # Each worker owns its decompression path; no shared scratch files.
            with tempfile.TemporaryDirectory(dir=temporary) as scratch:
                module = shipped
                if shipped.suffix in ('.xz', '.gz', '.zst'):
                    module = Path(scratch) / 'module.ko'
                    command = {'.xz': 'xz', '.gz': 'gzip', '.zst': 'zstd'}[shipped.suffix]
                    with module.open('wb') as stream:
                        subprocess.run([command, '-dc', str(shipped)], stdout=stream, check=True)
                identity, _, shipped_btf = elf_info(module, readelf)
                original = originals.get(identity)
                if original is None:
                    raise RuntimeError(f'no unstripped original for {label}/{shipped.relative_to(root)}')
                require_debug(original, readelf, btf=label in ('base', 'intel'))
                if label in ('base', 'intel') and not shipped_btf:
                    raise RuntimeError(f'shipped module lost BTF: {shipped}')
                name = f'modules/{label}/{shipped.relative_to(root)}'
                if shipped.suffix != '.ko':
                    name = name.rsplit('.', 1)[0]
                copy(original, name)
                return dict(path=name, build_id=identity, shipped_sha256=digest(shipped),
                            runtime_btf=shipped_btf)

        modules = timed('verify shipped modules', lambda: parallel_map(verify_module, tasks))
        # Kernel source URL/ref and compiler flags are in these resolved files.
        # Preserve command records without archiving the kernel signing key.
        for source in kernel.rglob('*.cmd'):
            copy(source, 'kernel-commands/' + str(source.relative_to(kernel)))
        metadata = dict(kernel_release=release, kernel_build_id=kernel_id, modules=modules,
                        reefy_commit=run('git', 'rev-parse', 'HEAD'),
                        buildroot_commit=run('git', 'ls-tree', 'HEAD', 'buildroot'),
                        compiler=run(prefix + 'gcc', '--version'),
                        binutils=run(readelf, '--version'),
                        pahole=run(str(output / 'host/bin/pahole'), '--version'))
        for checkout in ('amd-provider', 'artifact-fixtures'):
            metadata[checkout + '_commit'] = run('git', '-C', checkout, 'rev-parse', 'HEAD')
        (stage / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
        # Exact tracked build recipes/configs, without working-tree secrets.
        subprocess.run(['git', 'archive', '--format=tar', '-o', str(stage / 'reefy-source.tar'), 'HEAD'], check=True)
        paths = sorted(p for p in stage.rglob('*') if p.is_file())
        hashes = timed('hash bundle', lambda: parallel_map(digest, paths))
        manifest = {str(p.relative_to(stage)): value for p, value in zip(paths, hashes)}
        (stage / 'SHA256SUMS').write_text(''.join(f'{value}  {name}\n' for name, value in manifest.items()))
        timed('compress bundle', lambda: compress_bundle(stage, destination))
    print(destination)


if __name__ == '__main__':
    main()
