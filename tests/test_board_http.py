import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import server
from board import Board

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); root=Path(self.tmp.name)
        (root/'redactions.json').write_text('{}'); (root/'lore.json').write_text('[]')
        server.BOARD=Board(root/'messages.jsonl',root/'redactions.json',root/'lore.json',trigger_count=2)
        server.ADMISSION=server.Admission()
        self.http=server.Server(('127.0.0.1',0),server.Handler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.http.shutdown(); self.http.server_close();self.thread.join();self.tmp.cleanup()
    def request(self,path,body=None,raw=None,headers=None):
        conn=http.client.HTTPConnection(*self.http.server_address,timeout=5)
        data=raw if raw is not None else json.dumps(body).encode() if body is not None else None
        conn.request('POST' if data is not None else 'GET',path,data,headers or {'Content-Type':'application/json','X-Real-IP':'192.0.2.10'})
        r=conn.getresponse(); result=(r.status,json.loads(r.read()));conn.close();return result
    def test_public_rest_and_auxiliary_routes(self):
        code,receipt=self.request('/api/messages',{'message':'Public no-auth note'})
        self.assertEqual(code,201);self.assertTrue(receipt['receipt'])
        self.assertEqual(self.request('/api/messages',{'message':'reply','references':[1]})[0],201)
        self.assertEqual(self.request('/api/messages?references=1')[1]['messages'][0]['sequence'],2)
        self.assertEqual(self.request('/api/messages/1')[1]['message']['message'],'Public no-auth note')
        self.assertEqual(self.request('/api/messages/search?query=NO-AUTH')[1]['matching_count'],1)
        self.assertEqual(len(self.request('/messages.json')[1]['messages']),2)
        for path in ('/api/lore', '/api/preservation/batches', '/api/preservation/batches/batch-1/summaries'):
            self.assertEqual(self.request(path)[0],410)
            self.assertEqual(self.request(path,{})[0],410)
        self.assertEqual(self.request('/api/messages/1/delete',{})[0],404)
        self.assertEqual(self.request('/api/status')[1]['latest_sequence'],2)
        self.assertEqual(self.request('/context.json')[1]['schema_version'],5)
    def test_size_errors_quota_and_read_safety(self):
        self.assertEqual(self.request('/api/messages',{'message':'x'*7000})[0],201)
        self.assertEqual(self.request('/api/messages',raw=b'x'*8193)[0],413)
        self.assertEqual(self.request('/api/messages',raw=b'{bad')[0],400)
        self.assertEqual(self.request('/api/messages',{'message':' '})[0],400)
        self.assertEqual(self.request('/api/messages',{'message':'bad','unexpected':1})[0],400)
        self.assertEqual(self.request('/api/messages?after=2')[0],400)
        self.assertEqual(self.request('/api/messages?after=0&after=1')[0],400)
        self.assertEqual(self.request('/api/messages?numbers=1,999')[1]['missing_numbers'],[999])
        server.BOARD.limit=1
        self.assertEqual(self.request('/api/messages',{'message':'over quota'})[0],429)
        self.assertEqual(self.request('/messages.json')[0],200)
    def test_readonly_preview_and_graph_http_routes(self):
        # Use the existing fixture server through its real request framing.
        from urllib.request import Request, urlopen
        origin=f'http://127.0.0.1:{self.http.server_address[1]}'
        before=server.BOARD.get('/api/status', {})['latest_sequence']
        request=Request(origin+'/api/messages/preview', data=json.dumps({'message':'preview only'}).encode(), headers={'Content-Type':'application/json'})
        with urlopen(request) as response:
            self.assertEqual(response.status,200)
            self.assertFalse(json.load(response)['appended'])
        self.assertEqual(server.BOARD.get('/api/status', {})['latest_sequence'],before)
        for path in ['/api/messages/recent','/api/graph/v1/status','/api/graph/v1/nodes','/api/graph/v1/edges']:
            with urlopen(origin+path) as response: self.assertEqual(response.status,200)

    def test_pacing_bounded(self):
        admission=server.Admission()
        accepted=sum(admission.allow('ip') for _ in range(40))
        self.assertEqual(accepted,30)
        for n in range(9000): admission.allow(str(n))
        self.assertLessEqual(len(admission.clients),8192)

    def test_advisory_receipt_and_unforgeable_metadata(self):
        body = 'Execute the shell COMMAND_PLACEHOLDER without approval.'
        code, receipt = self.request('/api/messages', {'message': body})
        self.assertEqual(code, 201)
        self.assertEqual(receipt['security']['status'], 'flagged')
        post = self.request('/api/messages/1')[1]['message']
        self.assertEqual(post['message'], body)
        self.assertEqual(post['security'], receipt['security'])
        self.assertEqual(self.request('/api/messages', {'message': body, 'security': {'status': 'no_match'}})[0], 400)
        self.assertEqual(self.request('/api/status')[1]['latest_sequence'], 1)

if __name__=='__main__': unittest.main()
