"""One bounded correlated device RPC client; PUBACK is never an outcome."""
import json
import secrets
import threading
from reefy import rpc_codec as codec

LOCAL_FIELDS = {'type', 'type_version', 'request_id', 'instance_uuid', 'payload'}


def failure(code='temporarily_unavailable'):
    return {'error': {'code': code, 'message': codec.ERRORS[code],
                      'retryable': code == 'temporarily_unavailable',
                      'retry_after_seconds': 1 if code == 'temporarily_unavailable' else None}}


class DeviceRPC:
    def __init__(self, send, timeout=15, limit=8, send_legacy=None):
        self.send, self.timeout, self.limit = send, timeout, limit
        self.send_legacy = send_legacy
        self.lock = threading.Lock()
        self.pending = {}

    def request(self, payload, *, legacy=False):
        try:
            if (not isinstance(payload, dict) or set(payload) != LOCAL_FIELDS
                    or not isinstance(payload['payload'], dict)):
                raise codec.FrameError()
            frame = dict(payload, version=1, correlation_id=secrets.token_hex(16))
            codec.header(frame)
            # Canonical encoding validates Unicode, structure and size, and also
            # snapshots mutable caller-owned data before asynchronous completion.
            frame = codec.decode(codec.encode(frame))
            fingerprint = json.dumps([payload, legacy], sort_keys=True, separators=(',', ':'))
            if legacy and (self.send_legacy is None or frame['type'] not in ('catalog.publish', 'catalog.status')):
                raise codec.FrameError()
        except codec.FrameError:
            return failure('invalid_request')
        key = (frame['instance_uuid'], frame['request_id'])
        leader = False
        with self.lock:
            entry = self.pending.get(key)
            if entry:
                if entry['fingerprint'] != fingerprint:
                    return failure('conflict')
                entry['waiters'] += 1
            else:
                if len(self.pending) >= self.limit:
                    return failure()
                entry = {'frame': frame, 'legacy': legacy, 'fingerprint': fingerprint, 'event': threading.Event(),
                         'result': None, 'waiters': 1}
                self.pending[key] = entry
                leader = True
        try:
            if leader:
                if legacy:
                    body = dict(frame['payload']) if frame['type'] == 'catalog.publish' else {}
                    self.send_legacy(dict(body, instance_uuid=frame['instance_uuid'],
                        request_id=frame['request_id'], correlation_id=frame['correlation_id'],
                        action='publish' if frame['type'] == 'catalog.publish' else 'status'))
                else:
                    self.send(entry['frame'])
            if entry['event'].wait(self.timeout):
                return entry['result']
            return failure()
        except Exception:
            return failure()
        finally:
            with self.lock:
                entry['waiters'] -= 1
                if not entry['waiters']:
                    self.pending.pop(key, None)

    def receive(self, raw, *, retained=False):
        return self._receive(raw, retained=retained, legacy=False)

    def _receive(self, raw, *, retained=False, legacy=False):
        if retained:
            return False
        try:
            value = codec.decode(raw)
            header = codec.header(value)
            if set(value) not in (codec.HEADER | {'result'}, codec.HEADER | {'error'}):
                return False
            if 'result' in value:
                if not isinstance(value['result'], dict):
                    return False
            else:
                error = value['error']
                if (not isinstance(error, dict) or set(error) != {
                        'code', 'message', 'retryable', 'retry_after_seconds'}
                        or not isinstance(error['code'], str) or not 0 < len(error['code']) <= 64
                        or not isinstance(error['message'], str) or len(error['message']) > 512
                        or type(error['retryable']) is not bool
                        or (error['retry_after_seconds'] is not None and (
                            type(error['retry_after_seconds']) is not int
                            or not 1 <= error['retry_after_seconds'] <= 60))):
                    return False
        except codec.FrameError:
            return False
        key = (header['instance_uuid'], header['request_id'])
        with self.lock:
            entry = self.pending.get(key)
            if (not entry or entry['legacy'] != legacy or entry['event'].is_set()
                    or header != codec.header(entry['frame'])):
                return False
            entry['result'] = {key: value[key] for key in ('result', 'error') if key in value}
            entry['event'].set()
            return True


    def receive_legacy(self, payload):
        """Frame old command replies into the same pending table, never a second RPC."""
        try:
            payload = codec.decode(codec.encode(payload))
            if (not isinstance(payload, dict) or set(payload) != {
                    'action','instance_uuid','request_id','correlation_id','result'}
                    or payload['action'] != 'app_catalog_result'
                    or not isinstance(payload['result'], dict)):
                return False
            key = (payload['instance_uuid'], payload['request_id'])
            if not all(isinstance(value, str) for value in key):
                return False
            with self.lock:
                entry = self.pending.get(key)
                if (not entry or not entry['legacy']
                        or payload['correlation_id'] != entry['frame']['correlation_id']):
                    return False
                header = codec.header(entry['frame'])
            result = payload['result']
            if 'error' in result:
                retryable = result['error'] in ('temporarily_unavailable', 'rate_limited')
                outcome = {'error': {'code': result['error'], 'message': result.get('message', ''),
                    'retryable': retryable, 'retry_after_seconds': 1 if retryable else None}}
            else:
                outcome = {'result': result}
            return self._receive(codec.encode(dict(header, **outcome)), legacy=True)
        except (codec.FrameError, TypeError):
            return False
