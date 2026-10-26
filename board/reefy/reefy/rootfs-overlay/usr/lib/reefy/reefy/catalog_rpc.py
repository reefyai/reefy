"""Legacy local catalog API adapter over the common correlated RPC client."""

class CatalogRPC:
    def __init__(self, rpc):
        self.rpc = rpc

    def request(self, payload):
        if (not isinstance(payload, dict) or set(payload) - {
                'instance_uuid', 'request_id', 'action', 'manifest', 'app_id'}
                or payload.get('action') not in ('publish', 'status')):
            return {'error': 'invalid_request', 'message': 'Invalid catalog request'}
        status = payload['action'] == 'status'
        content = ({'publication_request_id': payload.get('request_id')} if status else
                   {key: payload[key] for key in ('manifest', 'app_id') if key in payload})
        reply = self.rpc.request({'type': 'catalog.status' if status else 'catalog.publish',
            'type_version': 1, 'request_id': payload.get('request_id'),
            'instance_uuid': payload.get('instance_uuid'), 'payload': content},
            legacy=self.rpc.send_legacy is not None)
        if 'result' in reply:
            return reply['result']
        error = reply['error']
        code = 'temporarily_unavailable' if error['code'] == 'rate_limited' else error['code']
        return {'error': code, 'message': error['message']}
