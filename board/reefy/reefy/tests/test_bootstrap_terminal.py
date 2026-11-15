"""Real OpenSSL proof generation without Python cryptography dependencies."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import _bootstrap
from reefy import bootstrap_terminal as terminal

DEVICE='11111111-1111-4111-8111-111111111111'
SIBLING='22222222-2222-4222-8222-222222222222'


class BootstrapTerminalProofTests(unittest.TestCase):
    def test_concurrent_registration_and_connect_preserve_one_private_key(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(terminal,'KEY_DIR',root):
            with ThreadPoolExecutor(max_workers=3) as workers:
                csrs=list(workers.map(terminal.registration_csr,[DEVICE]*3))
            self.assertEqual(len(set(csrs)),1)
            path=Path(terminal.key_path(DEVICE));saved=path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            first=terminal.connection_proof(DEVICE,'synthetic-owner')
            second=terminal.connection_proof(DEVICE,'synthetic-owner')
            decode=lambda data:base64.urlsafe_b64decode(data+'='*(-len(data)%4))
            one=json.loads(decode(first.split('.')[1]));two=json.loads(decode(second.split('.')[1]))
            self.assertNotEqual(one['jti'],two['jti'])
            self.assertEqual(one['exp']-one['iat'],300)
            self.assertEqual(one['sub'],DEVICE)
            self.assertEqual(one['iss'],'synthetic-owner')
            self.assertEqual(one['aud'],terminal.AUDIENCE)
            self.assertLessEqual(one['iat'],time.time())
            self.assertEqual(path.read_bytes(),saved)
            csr=Path(root)/'terminal.csr';csr.write_text(csrs[0])
            public=Path(root)/'terminal.pub'
            public.write_bytes(subprocess.check_output(['openssl','req','-in',str(csr),'-pubkey','-noout']))
            signature=Path(root)/'signature';signature.write_bytes(decode(first.split('.')[2]))
            verification=subprocess.run(['openssl','dgst','-sha256','-verify',str(public),'-signature',str(signature)],
                input=first.rsplit('.',1)[0].encode(),capture_output=True)
            self.assertEqual(verification.returncode,0)
            self.assertNotEqual(terminal.registration_csr(SIBLING),csrs[0])
            self.assertEqual(path.read_bytes(),saved)

    def test_invalid_uuid_cannot_create_key_files(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(terminal,'KEY_DIR',root):
            for value in ('../escape','',DEVICE+'/extra'):
                with self.assertRaises(ValueError):terminal.registration_csr(value)
            self.assertEqual(list(Path(root).iterdir()),[])


if __name__=='__main__':unittest.main()
