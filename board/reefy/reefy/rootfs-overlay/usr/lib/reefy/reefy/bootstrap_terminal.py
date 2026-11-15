"""Per-boot terminal proof, separate from the persistent adoption private key."""
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

KEY_DIR = '/run/reefy/bootstrap-terminal'
AUDIENCE = 'reefy:bootstrap-terminal'


def key_path(device_uuid):
    if not isinstance(device_uuid,str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',device_uuid):
        raise ValueError('Invalid bootstrap terminal identity')
    root = Path(KEY_DIR)
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    path = root/(device_uuid+'.key')
    # Control and terminal may start concurrently. Never rotate an existing
    # key on reconnect, and never expose a partly written PEM file.
    with (root/(device_uuid+'.lock')).open('a') as lock:
        os.chmod(lock.name,0o600)
        fcntl.flock(lock,fcntl.LOCK_EX)
        if not path.exists():
            fd, temporary = tempfile.mkstemp(dir=root,prefix='.key-')
            os.close(fd)
            try:
                subprocess.run(['openssl','genrsa','-out',temporary,'2048'],
                               check=True,capture_output=True,timeout=30)
                os.chmod(temporary,0o600)
                os.replace(temporary,path)
            finally:
                if os.path.exists(temporary):os.unlink(temporary)
    return str(path)


def registration_csr(device_uuid):
    result = subprocess.run(['openssl','req','-new','-key',key_path(device_uuid),
        '-subj','/O=Reefy/OU=Bootstrap terminal/CN=terminal'],
        check=True,capture_output=True,text=True,timeout=10)
    return result.stdout


def connection_proof(device_uuid, public_id):
    """A fresh one-use RS256 proof for every CONNECT, including reconnects."""
    now = int(time.time())
    encode = lambda data:base64.urlsafe_b64encode(data).rstrip(b'=')
    header = encode(b'{"alg":"RS256","typ":"JWT"}')
    payload = encode(json.dumps({'iss':public_id,'sub':device_uuid,'aud':AUDIENCE,
        'iat':now,'exp':now+300,'jti':secrets.token_hex(16)},separators=(',',':')).encode())
    message = header+b'.'+payload
    signature = subprocess.run(['openssl','dgst','-sha256','-sign',key_path(device_uuid)],
        input=message,check=True,capture_output=True,timeout=10).stdout
    return (message+b'.'+encode(signature)).decode()
