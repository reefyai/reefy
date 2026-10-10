"""Check a running test broker's ACL with MQTT 5 over TLS (stdlib only).

Uses disposable, non-retained enrollment/command messages. Run on a test broker.
"""
import argparse
from pathlib import Path
import socket
import ssl
import struct
import uuid


def varint(value):
    result = bytearray()
    while True:
        digit = value % 128
        value //= 128
        result.append(digit | (128 if value else 0))
        if not value:
            return bytes(result)


def utf8(value):
    data = value.encode()
    return struct.pack('!H', len(data)) + data


def receive(sock):
    def exact(size):
        data = b''
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError('Broker closed connection without an MQTT response')
            data += chunk
        return data

    header = exact(1)[0]
    size, multiplier = 0, 1
    for _ in range(4):
        digit = exact(1)[0]
        size += (digit & 127) * multiplier
        if not digit & 128:
            return header, exact(size)
        multiplier *= 128
    raise RuntimeError('Invalid MQTT remaining length')


def check(args, credential, action, topic, allowed):
    context = ssl.create_default_context(cafile=str(args.certs / 'ca.crt'))
    context.load_cert_chain(str(args.certs / f'{credential}.crt'),
                            str(args.certs / f'{credential}.key'))
    with socket.create_connection((args.host, args.port), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=args.host) as sock:
            def send(header, payload):
                sock.sendall(bytes([header]) + varint(len(payload)) + payload)

            # MQTT 5, clean start, no username/password: identity comes from cert CN.
            send(0x10, utf8('MQTT') + b'\x05\x02\x00\x0a\x00'
                 + utf8('acl-check-' + uuid.uuid4().hex))
            header, body = receive(sock)
            if header != 0x20 or len(body) < 3 or body[1] != 0:
                raise RuntimeError(f'{credential}: connection failed: {header:#x} {body.hex()}')
            if action == 'publish':
                send(0x32, utf8(topic) + b'\x00\x01\x00' + b'{}')
            else:
                send(0x82, b'\x00\x01\x00' + utf8(topic) + b'\x00')
            header, body = receive(sock)
            if header == 0xe0:
                granted = False
                if not body or body[0] != 0x87:
                    raise RuntimeError(f'Unexpected disconnect reason: {body.hex()}')
            elif action == 'publish' and header == 0x40 and body[:2] == b'\x00\x01':
                reason = body[2] if len(body) > 2 else 0
                if reason not in (0, 0x10, 0x87):
                    raise RuntimeError(f'Unexpected PUBACK reason: {reason:#x}')
                granted = reason < 0x80
            elif action == 'subscribe' and header == 0x90 and body[:2] == b'\x00\x01':
                # Skip the variable-length properties before reading SUBACK reason.
                index, props, multiplier = 2, 0, 1
                while True:
                    digit = body[index]
                    index += 1
                    props += (digit & 127) * multiplier
                    if not digit & 128:
                        break
                    multiplier *= 128
                reason = body[index + props]
                if reason not in (0, 0x87):
                    raise RuntimeError(f'Unexpected SUBACK reason: {reason:#x}')
                granted = reason == 0
            else:
                raise RuntimeError(f'Unexpected MQTT response: {header:#x} {body.hex()}')
            if granted != allowed:
                raise AssertionError(f'{credential} {action} {topic}: expected allowed={allowed}')
            print(f'PASS: {credential} {action} {topic}: allowed={granted}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--certs', type=Path, required=True)
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=8883)
    args = parser.parse_args()
    device = 'acl-check-' + uuid.uuid4().hex
    check(args, 'bootstrap', 'publish', f'reefy/devices/bootstrap/{device}/register', True)
    check(args, 'bootstrap', 'subscribe', f'reefy/devices/bootstrap/{device}/provision', True)
    check(args, 'bootstrap', 'publish', f'reefy/devices/{device}/commands', False)
    check(args, 'bootstrap', 'subscribe', f'reefy/devices/{device}/status', False)
    check(args, 'admin', 'publish', f'reefy/devices/{device}/commands', True)
    check(args, 'admin', 'subscribe', f'reefy/devices/{device}/status', True)
