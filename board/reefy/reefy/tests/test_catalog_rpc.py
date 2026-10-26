"""Catalog replies must be correlated and committed, not inferred from publish."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
import _bootstrap
from reefy.catalog_rpc import CatalogRPC

PAYLOAD={'instance_uuid':'abc00001','request_id':'synthetic-request','action':'publish','manifest':{'slug':'synthetic'}}
RESULT={'app_id':'a'*32,'release_id':'b'*32,'version':'1.0.0'}


class CatalogTests(unittest.TestCase):
    def test_roundtrip_and_wrong_instance(self):
        def send(payload):
            self.assertFalse(rpc.receive(dict(payload,instance_uuid='foreign',result=RESULT)))
            self.assertFalse(rpc.receive(dict(payload,correlation_id='0'*32,result=RESULT)))
            self.assertTrue(rpc.receive(dict(payload,result=RESULT)))
        rpc=CatalogRPC(send,timeout=.1)
        self.assertEqual(rpc.request(PAYLOAD),RESULT)
        self.assertFalse(rpc.receive(dict(PAYLOAD,result=RESULT)))
        self.assertEqual(rpc.pending,{})

    def test_transport_ack_does_not_complete(self):
        rpc=CatalogRPC(lambda payload: {'ok':True},timeout=.01)
        self.assertEqual(rpc.request(PAYLOAD)['error'],'temporarily_unavailable')
        self.assertEqual(rpc.pending,{})

    def test_concurrent_waiters_and_conflicting_reuse(self):
        sent=threading.Event()
        wire=[]
        def send(payload):
            wire.append(payload)
            sent.set()
        rpc=CatalogRPC(send,timeout=1)
        with ThreadPoolExecutor(max_workers=2) as workers:
            future=workers.submit(rpc.request,PAYLOAD)
            self.assertTrue(sent.wait(1))
            self.assertEqual(rpc.request(dict(PAYLOAD,app_id='different'))['error'],'conflict')
            self.assertTrue(rpc.receive(dict(wire[0],result=RESULT)))
            self.assertEqual(future.result(),RESULT)
        self.assertEqual(rpc.pending,{})

    def test_failure_and_invalid_input(self):
        def fail(payload):raise RuntimeError('private transport detail')
        rpc=CatalogRPC(fail,timeout=.01)
        self.assertEqual(rpc.request(PAYLOAD)['error'],'temporarily_unavailable')
        self.assertEqual(rpc.request(dict(PAYLOAD,user_id=2))['error'],'invalid_request')

    def test_late_reply_cannot_resolve_a_new_transport_attempt(self):
        sent=[];late_accepted=[]
        def send(payload):
            sent.append(dict(payload))
            if len(sent)==2:
                late_accepted.append(rpc.receive(dict(sent[0],result=RESULT)))
                rpc.receive(dict(payload,result={'error':'conflict','message':'Conflicting request'}))
        rpc=CatalogRPC(send,timeout=.01)
        assert rpc.request(PAYLOAD)['error']=='temporarily_unavailable'
        rpc.request(dict(PAYLOAD,manifest={'slug':'synthetic-changed'}))
        self.assertEqual(late_accepted,[False])

    def test_reply_from_before_control_restart_is_ignored(self):
        old=[]
        first=CatalogRPC(lambda payload:old.append(payload),timeout=.01)
        first.request(PAYLOAD)
        def send(payload):
            self.assertFalse(second.receive(dict(old[0],result=RESULT)))
            self.assertTrue(second.receive(dict(payload,result=RESULT)))
        second=CatalogRPC(send,timeout=.1)
        self.assertEqual(second.request(PAYLOAD),RESULT)

    def test_malformed_correlation_never_resolves_waiter(self):
        def send(payload):
            for invalid in (None, [], {}, 1):
                self.assertFalse(rpc.receive(dict(payload,correlation_id=invalid,result=RESULT)))
            self.assertFalse(rpc.receive(dict(payload,instance_uuid=[],result=RESULT)))
            self.assertTrue(rpc.receive(dict(payload,result=RESULT)))
        rpc=CatalogRPC(send,timeout=.1)
        self.assertEqual(rpc.request(PAYLOAD),RESULT)
        self.assertEqual(rpc.request(dict(PAYLOAD,request_id=[]))['error'],'invalid_request')
