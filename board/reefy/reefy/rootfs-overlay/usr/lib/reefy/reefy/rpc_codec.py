"""Bounded device RPC v1 framing. Payloads are data, never dispatch authority."""
from dataclasses import dataclass
import json
import re

MAX_BYTES = 64 * 1024
MAX_DEPTH = 16
MAX_NODES = 4096
INSTANCE = re.compile(r'(?:[0-9a-f]{8}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z')
REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
CORRELATION = re.compile(r'[0-9a-f]{32}\Z')
TYPE = re.compile(r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}\Z')
HEADER = frozenset({'version', 'type', 'type_version', 'request_id', 'correlation_id', 'instance_uuid'})
ERRORS = {
    'invalid_request': 'Invalid request', 'unsupported_version': 'Unsupported version',
    'unsupported_type': 'Unsupported request type', 'insufficient_scope': 'Not authorized',
    'not_found': 'Result not found', 'conflict': 'Request conflicts with an existing result',
    'rate_limited': 'Request limit reached', 'temporarily_unavailable': 'Request temporarily unavailable',
    'internal_error': 'Request outcome unavailable',
    'private_app_limit_reached': 'You can have up to 100 private apps per account.',
}


class FrameError(ValueError):
    def __init__(self, code='invalid_request'):
        self.code = code
        super().__init__(ERRORS[code])


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FrameError()
        result[key] = value
    return result


def _constant(_value):
    raise FrameError()


def structure(value):
    """Iterative traversal avoids a second recursion path over hostile input."""
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise FrameError()
        if isinstance(item, dict):
            count += len(item)  # Keys also consume the structural budget.
            if count > MAX_NODES or any(not isinstance(key, str) for key in item):
                raise FrameError()
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise FrameError()


def decode(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise FrameError()
    try:
        value = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=_pairs,
                           parse_constant=_constant)
        structure(value)
        # Validate Unicode scalar strings and finite numeric encoding too.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise FrameError() from None
    return value


def header(value):
    """Only this bounded subset may be reflected in an error response."""
    if not isinstance(value, dict) or not HEADER <= value.keys():
        raise FrameError()
    for field in ('version', 'type_version'):
        if type(value[field]) is not int or not 1 <= value[field] <= 65535:
            raise FrameError()
    for field, pattern in [('request_id', REQUEST_ID), ('correlation_id', CORRELATION),
                           ('instance_uuid', INSTANCE), ('type', TYPE)]:
        item = value[field]
        if not isinstance(item, str) or len(item) > 128 or not pattern.fullmatch(item):
            raise FrameError()
    return {key: value[key] for key in HEADER}


@dataclass(frozen=True)
class Handler:
    name: str
    version: int
    capability: str
    write: bool
    execute: object
    validate: object
    budget_seconds: int = 10
    result_max_bytes: int = MAX_BYTES - 2048

    def __post_init__(self):
        if (not TYPE.fullmatch(self.name) or type(self.version) is not int or self.version < 1
                or not isinstance(self.capability, str) or not self.capability
                or self.capability.endswith(':*') or type(self.write) is not bool
                or type(self.budget_seconds) is not int or not 1 <= self.budget_seconds <= 10
                or type(self.result_max_bytes) is not int or not 1 <= self.result_max_bytes <= MAX_BYTES - 2048
                or not callable(self.execute) or not callable(self.validate)):
            raise ValueError('Invalid RPC handler registration')


def request(value, registry):
    identity = header(value)
    if set(value) != HEADER | {'payload'} or not isinstance(value['payload'], dict):
        raise FrameError()
    if identity['version'] != 1:
        raise FrameError('unsupported_version')
    handler = registry.get(identity['type'])
    if handler is None:
        raise FrameError('unsupported_type')
    if identity['type_version'] != handler.version:
        raise FrameError('unsupported_version')
    handler.validate(value['payload'])
    return handler


def encode(value):
    try:
        structure(value)
        raw = json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) > MAX_BYTES:
            raise FrameError()
        return raw
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise FrameError() from None


def error_reply(identity, code, retry_after=None):
    if code not in ERRORS:
        code = 'internal_error'
    retryable = code in ('rate_limited', 'temporarily_unavailable')
    return {**header(identity), 'error': {
        'code': code, 'message': ERRORS[code], 'retryable': retryable,
        'retry_after_seconds': min(60, max(1, int(retry_after)))
        if retryable and retry_after is not None else None}}
