"""Local, RAM-only WiFi setup for devices awaiting adoption."""

import hashlib
import html
import http.server
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
from string import Template
from urllib.parse import parse_qs
import time

RUNTIME = Path('/run/reefy-wifi-bootstrap')
CREDENTIALS = RUNTIME / 'credentials.json'
IDENTITY = Path('/mnt/reefy-data/state/device-uuid')
NETWORK = Path('/run/systemd/network/10-reefy-wifi-bootstrap.network')
ADDRESS = '192.168.73.1'
PAGE = Path('/usr/share/reefy/wifi-setup.html')
CONNECT_TIMEOUT = 45


def command(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=20, **kwargs)


def private_write(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)
    os.replace(temporary, path)


def read_credentials():
    try:
        value = json.loads(CREDENTIALS.read_text())
        return validate_credentials(value)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def validate_credentials(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid WiFi settings')
    ssid, password = value.get('ssid'), value.get('password', '')
    if (not isinstance(ssid, str) or not 1 <= len(ssid.encode('utf-8')) <= 32
            or any(c in ssid for c in '\r\n\0')):
        raise ValueError('SSID must contain 1 to 32 bytes')
    if (not isinstance(password, str) or any(c in password for c in '\r\n\0')
            or not (8 <= len(password.encode('utf-8')) <= 63
                or re.fullmatch(r'[0-9a-fA-F]{64}', password))):
        raise ValueError('Password must contain 8 to 63 bytes, or a 64 digit hex key')
    return {'ssid': ssid, 'password': password}


def parse_scan(output):
    networks, item = {}, {}
    def save():
        ssid = item.get('ssid')
        if ssid and (ssid not in networks or item.get('signal', -1000) > networks[ssid].get('signal', -1000)):
            networks[ssid] = dict(item)
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith('BSS '):
            save()
            item = {'security': 'open'}
        elif line.startswith('SSID: '):
            # iw prints non-ASCII bytes and literal backslashes as \xNN.
            raw_ssid = re.sub(rb'\\x([0-9a-fA-F]{2})',
                              lambda match: bytes([int(match.group(1), 16)]),
                              line[6:].encode('utf-8'))
            try:
                item['ssid'] = raw_ssid.decode('utf-8')
            except UnicodeDecodeError:
                pass
        elif line.startswith('signal: '):
            try:
                item['signal'] = float(line.split()[1])
            except (ValueError, IndexError):
                pass
        elif line.startswith(('RSN:', 'WPA:')):
            item['security'] = 'secured'
    save()
    return sorted(networks.values(), key=lambda n: n.get('signal', -1000), reverse=True)


class Setup:
    def __init__(self, interface):
        if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', interface):
            raise ValueError('Invalid interface')
        self.interface = interface
        self.networks = []
        self.children = []
        self.server = None
        self.pending = None
        self.refresh = False
        self.lock = threading.Lock()
        self.stop = threading.Event()
        address = Path('/sys/class/net') / interface / 'address'
        suffix = hashlib.sha256(address.read_bytes()).hexdigest()[:4].upper()
        self.ssid = 'Reefy-Setup-' + suffix
        self.token = os.urandom(24).hex()

    def spawn(self, args):
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(process)
        return process

    def stop_children(self):
        for process in self.children:
            if process.poll() is None:
                process.terminate()
        for process in self.children:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.children.clear()

    def configure_network(self, ap):
        NETWORK.parent.mkdir(parents=True, exist_ok=True)
        # networkd runs as systemd-network, outside this service's private mask.
        NETWORK.parent.chmod(0o755)
        NETWORK.write_text('[Match]\nName=' + self.interface + '\n[Network]\n' + (
            'DHCP=no\nAddress=' + ADDRESS + '/24\nDNSDefaultRoute=no\n'
            if ap else 'DHCP=yes\nDNS=1.1.1.1\nDNS=8.8.8.8\n[DHCPv4]\nRouteMetric=600\n'))
        NETWORK.chmod(0o644)
        command(['networkctl', 'reload'])
        command(['networkctl', 'reconfigure', self.interface])

    def scan(self):
        command(['ip', 'link', 'set', self.interface, 'up'])
        result = command(['iw', 'dev', self.interface, 'scan'])
        if result.returncode == 0:
            self.networks = parse_scan(result.stdout)

    def connected(self):
        link = command(['iw', 'dev', self.interface, 'link'])
        addresses = command(['ip', '-j', '-4', 'addr', 'show', 'dev', self.interface])
        routes = command(['ip', '-4', 'route', 'show', 'default', 'dev', self.interface])
        try:
            has_address = any(a.get('scope') == 'global' and a.get('local') != ADDRESS
                              for d in json.loads(addresses.stdout) for a in d.get('addr_info', []))
        except (ValueError, TypeError):
            has_address = False
        return 'Connected to ' in link.stdout and has_address and bool(routes.stdout.strip())

    def connect(self, wifi):
        self.stop_children()
        self.configure_network(False)
        # Keep secrets out of argv, journals and persistent storage.
        ssid = wifi['ssid'].encode('utf-8').hex()
        password = wifi['password']
        network = 'network={\nssid=' + ssid + '\nscan_ssid=1\n'
        if password:
            psk = password if len(password) == 64 else hashlib.pbkdf2_hmac(
                'sha1', password.encode('utf-8'), wifi['ssid'].encode('utf-8'), 4096, 32).hex()
            network += 'psk=' + psk + '\n'
        else:
            network += 'key_mgmt=NONE\n'
        private_write(RUNTIME / 'client.conf', 'ctrl_interface=/run/wpa_supplicant\n' + network + '}\n')
        process = self.spawn(['wpa_supplicant', '-i', self.interface, '-D', 'nl80211', '-c', str(RUNTIME / 'client.conf')])
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while not self.stop.wait(1) and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if self.connected():
                private_write(CREDENTIALS, json.dumps(wifi))
                return True
        self.stop_children()
        CREDENTIALS.unlink(missing_ok=True)
        return False

    def serve(self):
        setup = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass

            def respond(self, status, body, content_type='text/html; charset=utf-8'):
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                options = []
                for network in setup.networks:
                    label = html.escape(network['ssid'], quote=True)
                    disabled = ' disabled' if network['security'] == 'open' else ''
                    options.append(f'<option value="{label}"{disabled}>{label}</option>')
                page = Template(PAGE.read_text()).substitute(
                    network_options=''.join(options), token=setup.token)
                self.respond(200, page.encode('utf-8'))

            def do_POST(self):
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 4096:
                        raise ValueError('Invalid request size')
                    values = parse_qs(self.rfile.read(size).decode('utf-8'), keep_blank_values=True)
                    if any(len(items) != 1 for items in values.values()):
                        raise ValueError('Duplicate form field')
                    data = {key: items[0] for key, items in values.items()}
                    if data.get('token') != setup.token:
                        self.respond(403, b'Reload the setup page.')
                        return
                    with setup.lock:
                        if setup.pending or setup.refresh:
                            self.respond(409, b'A connection attempt is already in progress.')
                            return
                        if self.path == '/connect':
                            setup.pending = validate_credentials(data)
                        elif self.path == '/refresh':
                            setup.refresh = True
                        else:
                            self.respond(404, b'Unknown setup action.')
                            return
                    message = (
                        'Connecting to Wi-Fi. Setup Wi-Fi will disconnect. If connection fails, reconnect to '
                        + setup.ssid + '.' if self.path == '/connect' else
                        'Scanning. Reconnect to ' + setup.ssid + ' and reload.')
                    page = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width,initial-scale=1">'
                            '<title>Reefy Wi-Fi setup</title><p role="status">'
                            + html.escape(message) + '</p></html>')
                    self.respond(202, page.encode('utf-8'))
                except (ValueError, TypeError, UnicodeError):
                    self.respond(400, b'Check the network name and password.')

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            def server_bind(self):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, setup.interface.encode() + b'\0')
                super().server_bind()
        self.server = Server((ADDRESS, 80), Handler)
        self.server.timeout = 1

    def access_point(self):
        self.stop_children()
        command(['systemctl', 'stop', 'wpa_supplicant@' + self.interface])
        self.scan()
        self.configure_network(True)
        private_write(RUNTIME / 'hostapd.conf',
                      'interface=' + self.interface + '\ndriver=nl80211\nssid=' + self.ssid
                      + '\nhw_mode=g\nchannel=6\nwpa=0\nap_isolate=1\n')
        private_write(RUNTIME / 'dnsmasq.conf',
                      'interface=' + self.interface + '\nexcept-interface=lo\nbind-interfaces\n'
                      'listen-address=' + ADDRESS + '\nuser=root\nno-resolv\nno-hosts\n'
                      'dhcp-range=192.168.73.10,192.168.73.50,255.255.255.0,5m\n'
                      'dhcp-option=3,' + ADDRESS + '\ndhcp-option=6,' + ADDRESS
                      + '\naddress=/#/' + ADDRESS + '\nlocal=/#/\n'
                      'dhcp-leasefile=' + str(RUNTIME / 'leases') + '\n')
        self.spawn(['hostapd', str(RUNTIME / 'hostapd.conf')])
        # networkd installs the AP address asynchronously.
        for _ in range(10):
            try:
                self.serve()
                break
            except OSError:
                if self.stop.wait(1):
                    return
        if self.server is None:
            raise RuntimeError('Setup address unavailable')
        self.spawn(['dnsmasq', '--keep-in-foreground', '--conf-file=' + str(RUNTIME / 'dnsmasq.conf')])
        print('[wifi-bootstrap] Setup AP ready', flush=True)
        while not self.stop.is_set():
            if any(p.poll() is not None for p in self.children):
                raise RuntimeError('Setup AP service stopped')
            self.server.handle_request()
            with self.lock:
                pending, refresh = self.pending, self.refresh
            if pending or refresh:
                # Let the browser receive the response before the radio switches.
                self.stop.wait(2)
                break
        self.server.server_close()
        self.server = None
        self.stop_children()
        with self.lock:
            wifi = self.pending
            self.pending, self.refresh = None, False
        if wifi and not self.stop.is_set():
            return self.connect(wifi)
        return False

    def run(self):
        try:
            previous = read_credentials()
            connected = bool(previous and self.connect(previous))
            while not self.stop.is_set():
                if connected:
                    self.stop.wait(2)
                else:
                    connected = self.access_point()
        finally:
            if self.server:
                self.server.server_close()
            self.stop_children()
            NETWORK.unlink(missing_ok=True)
            command(['networkctl', 'reload'])
            command(['networkctl', 'reconfigure', self.interface])
            # Credentials remain in RAM until successful desired-state handoff
            # or cleanup. A process restart must not destroy an adoption retry.
            for name in ('client.conf', 'hostapd.conf', 'dnsmasq.conf', 'leases'):
                (RUNTIME / name).unlink(missing_ok=True)


def main():
    if IDENTITY.exists():
        return
    # Allow existing Ethernet and early WiFi DHCP to finish first.
    for _ in range(15):
        if command(['ip', '-4', 'route', 'show', 'default']).stdout.strip():
            return
        time.sleep(1)
    from reefy.shared import find_wireless_iface
    interface = find_wireless_iface()
    if not interface:
        print('[wifi-bootstrap] No WiFi adapter', flush=True)
        return
    setup = Setup(interface)
    signal.signal(signal.SIGTERM, lambda *args: setup.stop.set())
    signal.signal(signal.SIGINT, lambda *args: setup.stop.set())
    setup.run()


if __name__ == '__main__':
    main()
