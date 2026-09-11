#!/usr/bin/env python3
"""Concurrent default-LV consumers cannot escape their quota domains."""
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
sys.path.insert(0, '/usr/lib/reefy')
from reefy.storage_admission import reservation, wait_generation
from reefy.storage_quota import Registry, RUN_DIR, command, read_quotas, physical_sample, state_lock
from reefy.storage_runtime import LAYER_INITIAL_SIZE

MIB = 1024**2
TAG = 'synthetic-runtime-import:probe'
HOST_FILE = Path('/mnt/reefy-data/cache/synthetic-runtime-fill')


class ZeroStream:
    def read(self, length):
        return bytes(length)


def image_archive(path):
    # A valid standalone Docker archive: real unpack work without network or a
    # build container. The uncompressed 96 MiB layer must exceed its test cap.
    with tempfile.TemporaryFile() as layer:
        with tarfile.open(fileobj=layer, mode='w') as archive:
            info = tarfile.TarInfo('synthetic-data')
            info.size = 96 * MIB
            archive.addfile(info, ZeroStream())
        length = layer.tell()
        layer.seek(0)
        digest = hashlib.file_digest(layer, 'sha256').hexdigest()
        config = json.dumps({'architecture': 'amd64', 'os': 'linux',
                             'config': {}, 'rootfs': {'type': 'layers',
                             'diff_ids': ['sha256:' + digest]}}).encode()
        name = hashlib.sha256(config).hexdigest() + '.json'
        manifest = json.dumps([{'Config': name, 'RepoTags': [TAG], 'Layers': ['layer.tar']}]).encode()
        with tarfile.open(path, 'w:gz') as archive:
            for filename, content in ((name, config), ('manifest.json', manifest)):
                info = tarfile.TarInfo(filename)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            info = tarfile.TarInfo('layer.tar')
            info.size = length
            layer.seek(0)
            archive.addfile(info, layer)


def run():
    assert Registry().data.get('active')
    HOST_FILE.parent.mkdir(parents=True, exist_ok=True)
    image = 'busybox:1.37.0'
    archive = '/tmp/synthetic-runtime-image.tar.gz'
    image_archive(archive)
    names = ['synthetic-runtime-layer-' + str(i) for i in range(3)] + ['synthetic-runtime-logs']
    streams, processes, trace, previous = [], [], [], {}
    try:
        with reservation('synthetic-runtime-create', len(names) * 2 * LAYER_INITIAL_SIZE):
            for name in names:
                script = ('dd if=/dev/zero bs=1M count=160 2>/dev/null | tr "\\000" x; sleep 300'
                          if name.endswith('logs') else
                          'i=0; while [ "$i" -lt 12 ]; do '
                          'dd if=/dev/zero of=/layer-fill bs=1M count=8 seek=$((i*8)) conv=notrunc 2>/dev/null; '
                          'i=$((i+1)); sleep 1; done; touch /done; sleep 300')
                command(['docker', 'create', '--name', name, image, 'sh', '-c', script])
        with state_lock():
            registry = Registry()
            for key, row in registry.data['projects'].items():
                if row['path'] in ('/mnt/reefy-data', '/mnt/reefy-data/docker/overlay2'):
                    previous[key] = row.get('max_hard')
                    used = read_quotas(row['mount'])[row['project']]['used']
                    row['max_hard'] = ((used + 64 * MIB + 4095) // 4096) * 4096
            assert len(previous) == 2
            registry.data['generation'] = registry.data.get('generation', 0) + 1
            generation = registry.data['generation']
            registry.save()
        wait_generation(generation)
        # These are deliberately naive host operations, outside managed pull
        # admission. Real root/overlay quotas must still bound their writes.
        command(['docker', 'start', *names])
        for args in (['docker', 'load', '-i', archive],
                     ['dd', 'if=/dev/zero', 'of=' + str(HOST_FILE), 'bs=1M', 'count=256', 'conv=fsync']):
            stream = tempfile.TemporaryFile(mode='w+')
            streams.append(stream)
            processes.append(subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT))
        started = time.monotonic()
        while time.monotonic() - started < 30 or any(p.poll() is None for p in processes):
            assert time.monotonic() - started < 120, 'runtime writers did not reach bounded errors'
            current = physical_sample()
            status = json.loads(Path(RUN_DIR, 'status.json').read_text())
            assert current.healthy
            assert current.capacity - current.used >= status['allocation']['boundaries']['emergency']
            assert not Path(RUN_DIR, 'hold.json').exists()
            trace.append({'seconds': time.monotonic() - started, 'physical_used': current.used})
            time.sleep(1)
        outputs = []
        for process, stream in zip(processes, streams):
            stream.seek(0)
            output = stream.read()
            outputs.append(output)
            assert process.returncode != 0, output
            assert any(word in output.lower() for word in ('no space left', 'quota exceeded')), output
        assert HOST_FILE.stat().st_size <= 64 * MIB
        for name in names[:-1]:
            info = json.loads(command(['docker', 'inspect', name]))[0]
            upper = Path(info['GraphDriver']['Data']['UpperDir'])
            assert (upper / 'layer-fill').stat().st_blocks * 512 > 0
            assert info['State']['Running']
        for key in previous:
            row = Registry().data['projects'][key]
            quota = read_quotas(row['mount'])[row['project']]
            assert 0 < quota['hard'] <= row['max_hard']
            assert quota['used'] <= quota['hard']
        state = Path('/mnt/reefy-data/apps/synthetic-recorder/config/runtime-independent')
        state.write_bytes(b's' * MIB)
        assert state.stat().st_size == MIB
        state.unlink()
        print(json.dumps({'concurrent_layers_logs_image_unpack_and_host_file': 'passed',
                          'quota_errors': outputs, 'samples': trace}))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        HOST_FILE.unlink(missing_ok=True)
        if previous:
            with state_lock():
                registry = Registry()
                for key, value in previous.items():
                    if value is None:
                        registry.data['projects'][key].pop('max_hard', None)
                    else:
                        registry.data['projects'][key]['max_hard'] = value
                registry.data['generation'] = registry.data.get('generation', 0) + 1
                generation = registry.data['generation']
                registry.save()
            wait_generation(generation)
        for name in names:
            subprocess.run(['docker', 'rm', '--force', name], capture_output=True, timeout=30)
        for stream in streams:
            stream.close()
        Path(archive).unlink(missing_ok=True)


if __name__ == '__main__':
    run()
