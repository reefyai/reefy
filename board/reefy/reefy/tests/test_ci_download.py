"""Exercise interrupted package downloads with real curl and a local server."""
import http.server
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest


SCRIPT = Path(__file__).resolve().parents[4] / 'tools/ci/download-package.sh'


@unittest.skipUnless(shutil.which('curl'), 'requires curl')
class PackageDownloadTests(unittest.TestCase):
    def exercise(self, recover):
        body = b'synthetic complete build package'
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body if recover and len(requests) > 1 else body[:4])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_WR)

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'package.deb'
            destination.write_bytes(b'previous complete package')
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run(
                    ['bash', str(SCRIPT),
                     f'http://127.0.0.1:{server.server_port}/package', str(destination)],
                    capture_output=True, timeout=20)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            self.assertFalse(list(Path(directory).glob('*.partial.*')))
            if recover:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(requests), 2)
                self.assertEqual(destination.read_bytes(), body)
            else:
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(requests), 4)
                self.assertEqual(destination.read_bytes(), b'previous complete package')

    def test_interrupted_receive_retries_without_appending_partial_bytes(self):
        self.exercise(recover=True)

    def test_exhausted_retries_preserve_previous_package_and_remove_partial_file(self):
        self.exercise(recover=False)
