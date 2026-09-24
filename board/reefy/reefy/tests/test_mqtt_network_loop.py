"""Exercise control's network-loop ownership with the real pinned Paho client."""
import json
import socket
import threading
import unittest
from unittest import mock

import _bootstrap  # noqa: F401

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None
else:
    from reefy.control import ControlPlane


@unittest.skipIf(mqtt is None, 'real Paho regression runs in its dedicated CI step')
class NetworkLoopTests(unittest.TestCase):
    def test_partial_write_cannot_interleave_worker_publications(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             reconnect_on_failure=False)
        left, right = socket.socketpair()
        left.setblocking(False)
        first_write = threading.Event()
        release_write = threading.Event()
        started = threading.Event()
        wire = bytearray()
        writers = set()
        failures = []

        class CaptureSocket:
            first = True

            def __getattr__(self, name):
                return getattr(left, name)

            def send(self, data):
                writers.add(threading.current_thread().ident)
                if self.first:
                    self.first = False
                    wire.extend(data[:1024])
                    first_write.set()
                    if not release_write.wait(5):
                        raise AssertionError('second publisher blocked behind socket write')
                    return 1024
                wire.extend(data)
                return len(data)

        client._sock = CaptureSocket()
        original_start = client.loop_start

        def start():
            result = original_start()
            started.set()
            return result

        def run():
            try:
                ControlPlane._run_network_loop(client)
            except BaseException as error:
                failures.append(error)

        with mock.patch.object(client, 'loop_start', side_effect=start):
            supervisor = threading.Thread(target=run, daemon=True)
            supervisor.start()
            try:
                self.assertTrue(started.wait(3))
                payloads = [json.dumps({'message': 'x' * 200000}),
                            json.dumps({'stage': 'ready'})]
                publications = []
                first = threading.Thread(target=lambda: publications.append(
                    client.publish('synthetic/status', payloads[0])))
                first.start()
                first.join(2)
                self.assertFalse(first.is_alive(), 'publisher wrote directly to socket')
                self.assertTrue(first_write.wait(3))
                second = threading.Thread(target=lambda: publications.append(
                    client.publish('synthetic/stage', payloads[1])))
                second.start()
                second.join(2)
                self.assertFalse(second.is_alive(), 'publisher waited on blocked socket')
                # While the network writer is deliberately blocked, both
                # publishing threads must return without writing any bytes.
                self.assertEqual(len(wire), 1024)
                release_write.set()
                for info in publications:
                    info.wait_for_publish(timeout=3)
                    self.assertTrue(info.is_published())
                self.assertEqual(len(writers), 1)
                self.assertNotIn(first.ident, writers)
                self.assertNotIn(second.ident, writers)

                decoded = []
                offset = 0
                while offset < len(wire):
                    self.assertEqual(wire[offset] & 0xf0, 0x30)
                    offset += 1
                    length, multiplier = 0, 1
                    while True:
                        value = wire[offset]
                        offset += 1
                        length += (value & 127) * multiplier
                        if not value & 128:
                            break
                        multiplier *= 128
                    end = offset + length
                    topic_length = int.from_bytes(wire[offset:offset+2], 'big')
                    decoded.append(json.loads(wire[offset+2+topic_length:end]))
                    offset = end
                self.assertEqual(decoded, [json.loads(p) for p in payloads])
            finally:
                release_write.set()
                client.disconnect()
                supervisor.join(5)
                left.close()
                right.close()
            self.assertFalse(supervisor.is_alive(), 'disconnect did not release supervisor')
            self.assertEqual(failures, [])

    def test_network_failure_releases_supervisor_for_client_recreation(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             reconnect_on_failure=False)
        with mock.patch.object(client, '_loop', return_value=mqtt.MQTT_ERR_CONN_LOST):
            supervisor = threading.Thread(
                target=ControlPlane._run_network_loop, args=(client,), daemon=True)
            supervisor.start()
            supervisor.join(3)
        self.assertFalse(supervisor.is_alive())
        self.assertIsNone(client._thread)

    def test_failed_thread_start_is_reported(self):
        client = mock.Mock()
        client.loop_start.return_value = mqtt.MQTT_ERR_INVAL
        with self.assertRaisesRegex(RuntimeError, 'could not start'):
            ControlPlane._run_network_loop(client)
        client._thread.join.assert_not_called()

    def test_thread_exit_before_handle_capture_is_cleaned_up(self):
        client = mock.Mock()
        client.loop_start.return_value = mqtt.MQTT_ERR_SUCCESS
        client._thread = None
        ControlPlane._run_network_loop(client)
        client.disconnect.assert_called_once()
        client.loop_stop.assert_called_once()
