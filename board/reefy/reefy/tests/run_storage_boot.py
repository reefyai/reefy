"""Host-side QMP interruptions of the actual first-boot quota migration."""
import json
from pathlib import Path
import shlex
import time


SNAPSHOT = '''import json, pathlib, subprocess
root = pathlib.Path('/run/reefy/storage-pressure')
def read(path):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return {}
registry = read(pathlib.Path('/mnt/reefy-data/state/storage-pressure/registry.json'))
print(json.dumps({'boot': pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
 'mount': read(root/'mount.json'), 'migration': read(root/'migration.json'),
 'record': next((r for r in registry.get('projects', {}).values() if r['path'] == '/mnt/reefy-data/apps/synthetic-scale/media'), {}),
 'pending': registry.get('activation_pending'), 'ready': (root/'session.json').exists(),
 'held': (root/'hold.json').exists(),
 'docker': int(subprocess.check_output(['systemctl','show','--property=MainPID','--value','docker.service'], timeout=2))}))
'''


def snapshot(vm):
    _, output, _ = vm.ssh_exec('python3 -c ' + shlex.quote(SNAPSHOT), timeout_s=8)
    return json.loads(output)


def run_boot_migration(vm, output):
    source = Path(__file__).with_name('boot_storage_probe.py')
    vm.scp_to(source, '/tmp/boot_storage_probe.py')
    _, fixture, _ = vm.ssh_exec('python3 /tmp/boot_storage_probe.py prepare', timeout_s=900)
    print(fixture)
    previous = snapshot(vm)['boot']
    results = {'fixture': json.loads(fixture.splitlines()[-1]), 'interruptions': []}
    project = None
    for phase in ('mounting', 'tagging', 'verifying', 'ready'):
        vm.qmp_execute('system_reset')
        started = time.monotonic()
        while time.monotonic() - started < 900:
            try:
                state = snapshot(vm)
            except Exception:
                time.sleep(1)
                continue
            if state['boot'] == previous:
                time.sleep(0.5)
                continue
            marker = state['mount'] if phase == 'mounting' else state['migration']
            if phase == 'ready':
                reached = state['ready'] and not state['held'] and not state['pending'] and state['docker'] > 1
            else:
                reached = marker.get('phase') == phase and marker.get('path') == '/mnt/reefy-data/apps/synthetic-scale/media'
                if phase == 'tagging':
                    reached = reached and marker.get('inodes', 0) >= 25000
            if reached:
                if phase != 'ready':
                    assert state['docker'] == 0, 'Docker started before migration verification completed'
                found = state['record'].get('project')
                if found is not None:
                    if project is not None:
                        assert found == project, 'interrupted migration changed the destination project'
                    project = found
                results['interruptions'].append({'phase': phase, 'seconds': time.monotonic() - started, 'state': state})
                previous = state['boot']
                print(f'Boot migration checkpoint {phase}: {time.monotonic() - started:.1f}s', flush=True)
                break
            if state['ready'] and state['docker'] > 1 and phase != 'ready':
                raise AssertionError(f'Boot completed before test observed required {phase} checkpoint')
            time.sleep(0.25)
        else:
            raise AssertionError(f'Boot did not reach {phase}')
    vm.scp_to(source, '/tmp/boot_storage_probe.py')
    _, verified, _ = vm.ssh_exec('python3 /tmp/boot_storage_probe.py verify', timeout_s=600)
    results['verification'] = json.loads(verified.splitlines()[-1])
    (output / 'boot-results.json').write_text(json.dumps(results, sort_keys=True, indent=2))
    print(verified)
