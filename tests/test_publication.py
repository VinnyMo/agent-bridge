import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('publisher', ROOT / 'scripts/publish.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class Publication(unittest.TestCase):
    def setUp(self):
        self.source_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.source_tmp.cleanup)
        self.source = Path(self.source_tmp.name)
        for relative in publisher.FILES + ('context.json',):
            dest = self.source / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            origin = ROOT / ('message-redactions.example.json' if relative == 'message-redactions.json' else relative)
            dest.write_bytes(origin.read_bytes())

    def test_staging_preserves_production_and_incompatible_rollback_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            published=Path(tmp)/'published'
            first=publisher.publish(self.source,published)
            marker=(published/'restart').read_bytes()
            registry=(published/'message-redactions.json').read_bytes()
            staged=publisher.publish(self.source,published,activate=False)
            self.assertNotEqual(first,staged)
            self.assertEqual((published/'current').resolve().name,first)
            self.assertEqual((published/'restart').read_bytes(),marker)
            self.assertEqual((published/'message-redactions.json').read_bytes(),registry)
            manifest=published/'releases'/first/'release.json'
            data=json.loads(manifest.read_text()); data.pop('write_protocol')
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'schema v5 identity history'):
                publisher.publish(self.source,published,rollback=first)

    def test_invalid_release_does_not_replace_current_and_rollback_preserves_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            source=base/'source'
            source.mkdir()
            for relative in publisher.FILES + ('context.json',):
                dest=source/relative
                dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_bytes((self.source/relative).read_bytes())
            published=base/'published'
            first=publisher.publish(source,published)
            original=(published/'current/context.json').read_bytes()
            (source/'context.json').write_text('{}')
            with self.assertRaises(ValueError):
                publisher.publish(source,published)
            self.assertEqual((published/'current').resolve().name,first)
            (source/'context.json').write_bytes((ROOT/'context.json').read_bytes())
            second=publisher.publish(source,published)
            self.assertNotEqual(first,second)
            self.assertEqual((published/'releases'/first/'context.json').read_bytes(),original)
            publisher.publish(source,published,rollback=first)
            self.assertEqual((published/'current').resolve().name,first)
            self.assertEqual((published/'restart').read_text().strip(),first)
            with self.assertRaises(ValueError):
                publisher.publish(source,published,rollback='../escape')
            (published/'releases'/first/'release.json').write_text(json.dumps({'release':first}))
            with self.assertRaisesRegex(ValueError, 'predates public-note redaction'):
                publisher.publish(source,published,rollback=first)

    def test_release_identity_and_public_post_cannot_replace_context(self):
        import socket
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            release=publisher.publish(self.source,base/'published')
            folder=base/'published/releases'/release
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0))
                port=sock.getsockname()[1]
            env={**os.environ,'AGENT_RATE_SALT':'a'*64,'AGENT_PORT':str(port),'AGENT_PUBLIC_WRITES':'1',
                 'AGENT_BIND':'127.0.0.1','AGENT_MESSAGES_FILE':str(base/'messages.jsonl'),
                 'AGENT_ABUSE_DB_FILE':str(base/'abuse.sqlite3'),
                 'AGENT_CONTEXT_FILE':str(base/'wrong-context.json')}
            proc=subprocess.Popen(['python3',str(folder/'server.py')],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                origin=f'http://127.0.0.1:{port}'
                publisher.wait_for_release(release,origin,timeout=8)
                with urlopen(origin+'/') as response:
                    page=response.read().decode()
                    for path in ('/context.json', '/messages.json', '/openapi.yaml'):
                        self.assertIn(path,page)
                    self.assertIn('8 KiB',page)
                with urlopen(origin+'/openapi.yaml') as response:
                    self.assertEqual(response.status,200)
                    self.assertIn(b'appendAgentMessage',response.read())
                before=(folder/'context.json').read_bytes()
                bad=Request(origin+'/api/messages',data=json.dumps({'message':'test','projects':[]}).encode(),headers={'Content-Type':'application/json'})
                with self.assertRaises(HTTPError) as error:
                    urlopen(bad)
                self.assertEqual(error.exception.code,400)
                good=Request(origin+'/api/messages',data=json.dumps({'agent':'Test','message':'[General] Temporary local test.'}).encode(),headers={'Content-Type':'application/json'})
                with urlopen(good) as response:
                    self.assertEqual(response.status,201)
                    receipt=json.load(response)
                    self.assertEqual(receipt['status'],'appended')
                self.assertEqual((folder/'context.json').read_bytes(),before)
                stored=(base/'messages.jsonl').read_bytes()
                registry=base/'published/message-redactions.json'
                registry.write_text(json.dumps({receipt['id']:'[General] Reviewed public result.'}))
                with urlopen(origin+'/messages.json') as response:
                    note=json.load(response)['messages'][-1]
                    self.assertEqual(note['message'],'[General] Reviewed public result.')
                    self.assertTrue(note['redacted'])
                self.assertEqual((base/'messages.jsonl').read_bytes(),stored)
                registry.write_text('{broken')
                with self.assertRaises(HTTPError) as error:
                    urlopen(origin+'/messages.json')
                self.assertEqual(error.exception.code,503)
            finally:
                proc.terminate()
                proc.communicate(timeout=5)


if __name__=='__main__':
    unittest.main()
