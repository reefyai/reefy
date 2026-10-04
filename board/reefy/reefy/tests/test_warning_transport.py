"""Exercise the installed warning schema over a real Varlink socket."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

import _bootstrap  # noqa: F401
from reefy.apply_results import ApplyResultStore, apply_warning
try:
    import varlink
except ImportError:
    varlink = None


@unittest.skipIf(varlink is None, 'install varlink==31.0.0 (required in CI)')
class WarningTransportTests(unittest.TestCase):
    def test_all_subjects_survive_get_and_wait_on_the_wire(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            store = ApplyResultStore(str(Path(directory) / 'results'))
            request_id = str(uuid.uuid4())
            self.assertTrue(store.create(request_id, 'apply'))
            warnings = [apply_warning('synthetic.failed', 'Synthetic failure', kind, identity)
                        for kind, identity in [('policy', 'host.policies.hardware.nvme.apst'),
                                               ('volume', 'synthetic-app/media'),
                                               ('app', 'synthetic-app'),
                                               ('service', 'synthetic-app/worker'),
                                               ('project', 'reefy-system')]]
            self.assertTrue(store.update(request_id, 'succeeded_with_warnings',
                                         warnings=warnings, applied=True))
            stored = store.get(request_id)
            result = {key: stored[key] for key in
                      ('request_id', 'status', 'error', 'warnings', 'applied')}
            result['found'] = True
            interface_dir = Path(__file__).resolve().parents[1] / 'rootfs-overlay/usr/share/varlink'
            service = varlink.Service(interface_dir=str(interface_dir))

            @service.interface('io.reefy.Reconciler')
            class Reconciler:
                def GetApply(self, request_id, _more=False):
                    return result

                def WaitApply(self, request_id, _more=False):
                    return result

            class Handler(varlink.RequestHandler):
                pass
            Handler.service = service
            address = 'unix:' + directory + '/test.sock'
            with varlink.ThreadingServer(address, Handler) as server:
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    with varlink.Client.new_with_address(address) as client:
                        with client.open('io.reefy.Reconciler') as api:
                            self.assertEqual(api.GetApply(request_id=request_id), result)
                            self.assertEqual(api.WaitApply(request_id=request_id), result)
                finally:
                    server.shutdown()
                    worker.join(timeout=5)

    def test_old_persisted_results_migrate_on_load(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ApplyResultStore(directory)
            request_id = str(uuid.uuid4())
            store.create(request_id, 'apply')
            path = Path(directory) / (request_id + '.json')
            record = json.loads(path.read_text())
            record.update(status='succeeded_with_warnings', warnings=[
                {'code': 'storage.cap_not_enforced', 'instance_uuid': 'app-a', 'volume': 'media'},
                {'code': 'app_project_failed', 'instance_uuid': 'app-b', 'volume': ''},
                {'code': 'system_project_failed', 'instance_uuid': '', 'volume': ''},
                {'code': 'policy.apply_failed', 'policy': 'host.policies.hardware.nvme.apst',
                 'error': 'cannot write /sys/private-node'},
            ])
            path.write_text(json.dumps(record))
            migrated = ApplyResultStore(directory).get(request_id)['warnings']
            self.assertEqual([w['subject']['kind'] for w in migrated],
                             ['volume', 'app', 'project', 'policy'])
            self.assertEqual(migrated[-1]['message'], 'cannot write [PATH]')
            self.assertTrue(all(set(w) == {'code', 'message', 'subject'} for w in migrated))
