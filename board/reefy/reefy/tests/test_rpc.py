"""Common RPC correlation, hostile responses and bounded resource lifetime."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
import _bootstrap
from reefy.rpc import DeviceRPC
from reefy import rpc_codec as codec

PAYLOAD = {'type': 'catalog.publish', 'type_version': 1, 'instance_uuid': 'abc00001',
           'request_id': 'synthetic-request', 'payload': {'manifest': {'slug': 'synthetic'}}}
RESULT = {'app_id': 'a'*32, 'release_id': 'b'*32, 'version': '1.0.0'}


def reply(frame, **outcome):
    return codec.encode(dict(codec.header(frame), **(outcome or {'result': RESULT})))


class RPCTests(unittest.TestCase):
    def test_only_exact_current_reply_completes(self):
        def send(frame):
            for field, value in [('instance_uuid', 'abc00002'), ('request_id', 'other'),
                                 ('correlation_id', '0'*32), ('type', 'catalog.status'),
                                 ('type_version', 2), ('version', 2)]:
                self.assertFalse(rpc.receive(reply(dict(frame, **{field: value}))))
            self.assertFalse(rpc.receive(reply(frame), retained=True))
            self.assertTrue(rpc.receive(reply(frame)))
            self.assertFalse(rpc.receive(reply(frame)))
        rpc = DeviceRPC(send, timeout=.1)
        self.assertEqual(rpc.request(PAYLOAD), {'result': RESULT})
        self.assertEqual(rpc.pending, {})

    def test_publish_ack_never_implies_domain_success(self):
        rpc = DeviceRPC(lambda _: True, timeout=.001)
        self.assertEqual(rpc.request(PAYLOAD)['error']['code'], 'temporarily_unavailable')
        self.assertEqual(rpc.pending, {})

    def test_conflicting_reuse_is_rejected_and_equivalent_waiters_share_send(self):
        sent = threading.Event(); wire = []
        def send(frame):
            wire.append(frame); sent.set()
        rpc = DeviceRPC(send, timeout=1)
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(rpc.request, PAYLOAD)
            self.assertTrue(sent.wait(1))
            self.assertEqual(rpc.request(dict(PAYLOAD, payload={}))['error']['code'], 'conflict')
            second = workers.submit(rpc.request, dict(reversed(list(PAYLOAD.items()))))
            # Both calls may share a live waiter or the second may start after
            # completion; wait for it to join deterministically before delivery.
            import time
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with rpc.lock:
                    if next(iter(rpc.pending.values()))['waiters'] == 2:
                        break
                time.sleep(.001)
            with rpc.lock:
                self.assertEqual(next(iter(rpc.pending.values()))['waiters'], 2)
            self.assertEqual(len(wire), 1)
            self.assertTrue(rpc.receive(reply(wire[0])))
            self.assertEqual(first.result(), {'result': RESULT})
            self.assertEqual(second.result(), {'result': RESULT})
        self.assertEqual(rpc.pending, {})

    def test_late_reply_and_control_restart_cannot_resolve_new_attempt(self):
        old = []
        first = DeviceRPC(old.append, timeout=.001)
        first.request(PAYLOAD)
        def send(frame):
            self.assertFalse(second.receive(reply(old[0])))
            self.assertTrue(second.receive(reply(frame)))
        second = DeviceRPC(send, timeout=.1)
        self.assertEqual(second.request(PAYLOAD), {'result': RESULT})

    def test_same_client_retry_ignores_earlier_correlation(self):
        sent = []
        def send(frame):
            sent.append(frame)
            if len(sent) == 2:
                self.assertFalse(rpc.receive(reply(sent[0])))
                self.assertTrue(rpc.receive(reply(frame)))
        rpc = DeviceRPC(send, timeout=.001)
        self.assertIn('error', rpc.request(PAYLOAD))
        self.assertEqual(rpc.request(PAYLOAD), {'result': RESULT})
        self.assertNotEqual(sent[0]['correlation_id'], sent[1]['correlation_id'])

    def test_hostile_inputs_and_replies_do_not_resolve(self):
        def send(frame):
            for raw in [b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff', b'['*1100+b']'*1100,
                        b' '*65537, reply(frame, result=[]), reply(frame, error={}),
                        codec.encode(dict(codec.header(frame), result={}, error={})),
                        reply(frame, result={}, unexpected=True)]:
                self.assertFalse(rpc.receive(raw))
            self.assertTrue(rpc.receive(reply(frame)))
        rpc = DeviceRPC(send, timeout=.1)
        self.assertEqual(rpc.request(PAYLOAD), {'result': RESULT})
        for value in [None, [], dict(PAYLOAD, account='foreign'),
                      dict(PAYLOAD, correlation_id='0'*32), dict(PAYLOAD, request_id=[]),
                      dict(PAYLOAD, payload={'x': '\ud800'})]:
            self.assertEqual(rpc.request(value)['error']['code'], 'invalid_request')

    def test_transport_failure_and_capacity_release(self):
        def fail(_): raise RuntimeError('synthetic transport secret')
        rpc = DeviceRPC(fail, timeout=.001)
        self.assertEqual(rpc.request(PAYLOAD)['error']['code'], 'temporarily_unavailable')
        self.assertEqual(rpc.pending, {})
        rpc.limit = 0
        self.assertEqual(rpc.request(PAYLOAD)['error']['code'], 'temporarily_unavailable')
        self.assertEqual(rpc.pending, {})

    def test_new_registered_type_uses_same_transport_without_command_dispatch(self):
        def send(frame):
            self.assertEqual(frame['type'], 'synthetic.echo')
            self.assertTrue(rpc.receive(reply(frame, result={'echo': 'synthetic'})))
        rpc = DeviceRPC(send, timeout=.1)
        self.assertEqual(rpc.request(dict(PAYLOAD, type='synthetic.echo')),
                         {'result': {'echo': 'synthetic'}})
