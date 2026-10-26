"""Bounded correlated catalog RPC. Transport acknowledgements are not results."""
import json
import re
import secrets
import threading


class CatalogRPC:
    def __init__(self, send, timeout=15, limit=8):
        self.send, self.timeout, self.limit = send, timeout, limit
        self.lock = threading.Lock()
        self.pending = {}

    def request(self, payload):
        if (not isinstance(payload, dict) or len(json.dumps(payload).encode()) > 64 * 1024
                or set(payload) - {'instance_uuid','request_id','action','manifest','app_id'}
                or not isinstance(payload.get('instance_uuid'),str)
                or not isinstance(payload.get('request_id'),str)
                or not re.fullmatch('[A-Za-z0-9_-]{1,128}',payload['request_id'])
                or payload.get('action') not in ('publish','status')):
            return {'error':'invalid_request','message':'Invalid catalog request'}
        key=(payload['instance_uuid'],payload['request_id'])
        fingerprint=json.dumps(payload,sort_keys=True,separators=(',',':'))
        leader=False
        with self.lock:
            entry=self.pending.get(key)
            if entry:
                if entry['fingerprint']!=fingerprint:
                    return {'error':'conflict','message':'Request is already in flight'}
                entry['waiters']+=1
            else:
                if len(self.pending)>=self.limit:
                    return {'error':'temporarily_unavailable','message':'Catalog requests busy'}
                entry={'event':threading.Event(),'fingerprint':fingerprint,'result':None,'waiters':1,
                       'correlation_id':secrets.token_hex(16)}
                self.pending[key]=entry
                leader=True
        try:
            if leader:
                self.send(dict(payload,correlation_id=entry['correlation_id']))
            if entry['event'].wait(self.timeout):
                return entry['result']
            return {'error':'temporarily_unavailable','message':'Retry the same request after timeout'}
        except Exception:
            return {'error':'temporarily_unavailable','message':'Catalog transport unavailable'}
        finally:
            with self.lock:
                entry['waiters']-=1
                if not entry['waiters']:
                    self.pending.pop(key,None)

    def receive(self,payload):
        if (not isinstance(payload,dict) or not isinstance(payload.get('result'),dict)
                or not all(isinstance(payload.get(k),str)
                           for k in ('instance_uuid','request_id','correlation_id'))):
            return False
        key=(payload.get('instance_uuid'),payload.get('request_id'))
        with self.lock:
            entry=self.pending.get(key)
            if (not entry or entry['event'].is_set()
                    or payload['correlation_id'] != entry['correlation_id']):
                return False
            entry['result']=payload['result']
            entry['event'].set()
            return True
