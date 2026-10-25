"""The unchanged local catalog API adapts to the single common RPC client."""
import unittest
import _bootstrap
from reefy.catalog_rpc import CatalogRPC
from reefy.rpc import DeviceRPC
from reefy import rpc_codec as codec

PAYLOAD = {'instance_uuid':'abc00001','request_id':'synthetic-request',
           'action':'publish','manifest':{'slug':'synthetic'}}
RESULT = {'app_id':'a'*32,'release_id':'b'*32,'version':'1.0.0'}


def legacy_reply(payload):
    return dict({key: payload[key] for key in ('instance_uuid', 'request_id', 'correlation_id')},
                action='app_catalog_result', result=RESULT)


class CatalogTests(unittest.TestCase):
    def test_publish_and_status_preserve_local_results(self):
        sent = []
        def send(frame):
            sent.append(frame)
            rpc.receive(codec.encode(dict(codec.header(frame), result=RESULT)))
        rpc = DeviceRPC(send, timeout=.1)
        catalog = CatalogRPC(rpc)
        self.assertEqual(catalog.request(PAYLOAD), RESULT)
        self.assertEqual(sent[0]['type'], 'catalog.publish')
        self.assertEqual(sent[0]['payload'], {'manifest': PAYLOAD['manifest']})
        self.assertEqual(catalog.request(dict(PAYLOAD, action='status', app_id=None)), RESULT)
        self.assertEqual(sent[1]['type'], 'catalog.status')
        self.assertEqual(sent[1]['payload'], {'publication_request_id':'synthetic-request'})
        self.assertEqual(rpc.pending, {})

    def test_quota_preserves_existing_code_and_message(self):
        def send(frame):
            rpc.receive(codec.encode(codec.error_reply(frame, 'private_app_limit_reached')))
        rpc = DeviceRPC(send, timeout=.1)
        self.assertEqual(CatalogRPC(rpc).request(PAYLOAD), {
            'error':'private_app_limit_reached',
            'message':'You can have up to 100 private apps per account.'})

    def test_timeout_is_not_reported_as_publication_success(self):
        rpc = DeviceRPC(lambda _: True, timeout=.001)
        self.assertEqual(CatalogRPC(rpc).request(PAYLOAD)['error'], 'temporarily_unavailable')
        self.assertEqual(rpc.pending, {})

    def test_invalid_local_requests_never_reach_transport(self):
        def fail(_): self.fail('invalid request reached transport')
        catalog = CatalogRPC(DeviceRPC(fail, timeout=.001))
        for value in [None, [], dict(PAYLOAD, user_id=2), dict(PAYLOAD, request_id=[])]:
            self.assertEqual(catalog.request(value)['error'], 'invalid_request')


    def test_legacy_wire_uses_common_waiter_and_cannot_be_completed_by_generic_reply(self):
        sent = []
        def send_legacy(payload):
            sent.append(payload)
            entry = next(iter(rpc.pending.values()))
            self.assertFalse(rpc.receive(codec.encode(dict(codec.header(entry['frame']), result=RESULT))))
            self.assertTrue(rpc.receive_legacy(legacy_reply(payload)))
        def forbidden(_): self.fail('legacy catalog unexpectedly used generic wire')
        rpc = DeviceRPC(forbidden, send_legacy=send_legacy, timeout=.1)
        catalog = CatalogRPC(rpc)
        self.assertEqual(catalog.request(PAYLOAD), RESULT)
        self.assertEqual(set(sent[0]), set(PAYLOAD) | {'correlation_id'})
        self.assertEqual(sent[0]['action'], 'publish')
        self.assertEqual(rpc.pending, {})

    def test_legacy_retry_rejects_late_reply_from_previous_attempt(self):
        sent = []
        def send(payload):
            sent.append(payload)
            if len(sent) == 2:
                self.assertFalse(rpc.receive_legacy(legacy_reply(sent[0])))
                self.assertTrue(rpc.receive_legacy(legacy_reply(payload)))
        rpc = DeviceRPC(lambda _: None, send_legacy=send, timeout=.001)
        catalog = CatalogRPC(rpc)
        self.assertEqual(catalog.request(PAYLOAD)['error'], 'temporarily_unavailable')
        self.assertEqual(catalog.request(PAYLOAD), RESULT)
        self.assertNotEqual(sent[0]['correlation_id'], sent[1]['correlation_id'])
        self.assertEqual(rpc.pending, {})
