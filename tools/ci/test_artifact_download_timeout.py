"""Opt-in integration probe for the exact published artifact downloader bundle.

Run with REEFY_ARTIFACT_ACTION_BUNDLE pointing to the immutable action's
dist/index.js. No GitHub credentials, network, device or archive data is used.
The stream extractor is replaced by a writable fixture; full firmware E2E
continues to validate real archives, signatures and release publication.
"""
import json
import os
from pathlib import Path
import subprocess
import unittest

HELPER = """function __awaiter(thisArg,_arguments,P,generator){return new Promise((resolve,reject)=>{function step(result){result.done?resolve(result.value):Promise.resolve(result.value).then(fulfilled,rejected)}function fulfilled(v){try{step(generator.next(v))}catch(e){reject(e)}}function rejected(v){try{step(generator.throw(v))}catch(e){reject(e)}}generator=generator.apply(thisArg,_arguments||[]);step(generator.next())})}const download_artifact_awaiter=__awaiter;"""


def probe(bundle, mode):
    text = Path(bundle).read_text()
    start = text.index('function streamExtractExternal(')
    end = text.index('\nfunction downloadArtifactPublic(', start)
    fn = text[start:end].replace('exports.streamExtractExternal = streamExtractExternal;', '')
    code = """const stream=require('stream'),crypto=require('crypto'),path=require('path'),fs=require('fs');
const response=new stream.PassThrough();
response.statusCode=200;
response.headers={'content-type':'application/zip'};
response.on('error',()=>{});
const client={HttpClient:class{async get(){return {message:response}}}};
const httpClient=client,lib_HttpClient=client.HttpClient;
const core={debug:()=>{},info:()=>{}};
const core_debug=core.debug,info=core.info;
const getUserAgentString=()=>'',user_agent_1={getUserAgentString};
const external_crypto_namespaceObject=crypto,external_stream_=stream,external_path_=path,external_fs_=fs;
const unzip={Extract:()=>new stream.Writable({write(c,e,cb){cb()}})},unzip_stream_1={default:unzip};
const nativeTimeout=setTimeout;
setTimeout=(fn,ms)=>nativeTimeout(fn,Math.min(ms,50));""" + HELPER + fn + "\nstreamExtractExternal('http://fixture.invalid/input.zip','/unused').then(()=>console.log(JSON.stringify({completed:true})),e=>console.log(JSON.stringify({rejected:true,message:e.message})));"
    code += "\nsetImmediate(()=>response.end('complete synthetic archive'));" if mode=='complete' else "\nsetImmediate(()=>response.write('partial archive'));"
    result = subprocess.run(['node', '-e', code], capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout) if result.stdout.strip() else None


@unittest.skipUnless(os.environ.get('REEFY_ARTIFACT_ACTION_BUNDLE'), 'requires the exact published action bundle')
class ArtifactDownloadTimeoutTests(unittest.TestCase):
    def test_complete_transfer_resolves(self):
        result = probe(os.environ['REEFY_ARTIFACT_ACTION_BUNDLE'], 'complete')
        self.assertTrue(result and result.get('completed'))

    def test_stalled_transfer_rejects_instead_of_false_success(self):
        result = probe(os.environ['REEFY_ARTIFACT_ACTION_BUNDLE'], 'stalled')
        self.assertTrue(result and result.get('rejected'), 'stalled download exited with an unresolved promise')
        self.assertIn('did not respond', result['message'])


if __name__ == '__main__':
    unittest.main()
