"""Run with the installed MCP Python; all messages use a temporary local board."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import httpx
from fastmcp import Client
import server
import mcp_bridge
from board import Board

class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); root=Path(self.tmp.name)
        (root/'redactions.json').write_text('{}'); (root/'lore.json').write_text('[]')
        server.BOARD=Board(root/'messages.jsonl',root/'redactions.json',root/'lore.json',trigger_count=2)
        server.ADMISSION=server.Admission()
        self.http=server.Server(('127.0.0.1',0),server.Handler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.base=patch.object(mcp_bridge,'BASE',f'http://127.0.0.1:{self.http.server_address[1]}');self.base.start()
        self.identity=patch.object(mcp_bridge,'get_http_headers',return_value={'x-real-ip':'192.0.2.20'});self.identity.start()
    def tearDown(self):
        self.identity.stop();self.base.stop();self.http.shutdown();self.http.server_close();self.thread.join();self.tmp.cleanup()
    async def test_tools_and_rest_share_results(self):
        async with Client(mcp_bridge.mcp) as client:
            names={t.name for t in await client.list_tools()}
            self.assertEqual(len(names),15)
            async def call(name,**args):
                result=await client.call_tool(name,args)
                return result.data
            a=await call('post_agent_message',message='x'*500,agent='MCP test')
            self.assertEqual(a['sequence'],1);self.assertTrue(a['receipt'])
            b=await call('post_agent_message',message='literal Search phrase',references=[1])
            self.assertEqual(b['sequence'],2)
            self.assertEqual((await call('get_agent_updates',after=0,limit=1))['next_cursor'],1)
            self.assertEqual((await call('search_agent_messages',query='SEARCH phrase'))['matching_count'],1)
            self.assertEqual((await call('get_agent_posts',numbers=[1,2]))['matching_count'],2)
            self.assertEqual((await call('get_post_references',number=1))['matching_count'],1)
            self.assertEqual((await call('get_preservation_batches'))['batches'][0]['id'],'batch-1')
            self.assertEqual((await call('get_batch_posts',batch_id='batch-1'))['messages'][0]['sequence'],1)
            self.assertEqual((await call('volunteer_for_batch',batch_id='batch-1'))['http_status'],201)
            self.assertEqual((await call('submit_lore_summary',batch_id='batch-1',summary='A public-safe interpretation.'))['http_status'],201)
            self.assertEqual((await call('get_batch_volunteers',batch_id='batch-1'))['matching_count'],1)
            self.assertEqual((await call('get_batch_summaries',batch_id='batch-1'))['matching_count'],1)
            self.assertEqual((await call('get_board_status'))['latest_sequence'],4)
            self.assertEqual(len((await call('get_agent_messages'))['messages']),4)
            self.assertEqual((await call('get_lore'))['entries'],[])
            self.assertEqual((await call('get_agent_context'))['schema_version'],4)
            server.BOARD.limit=4
            with self.assertRaises(Exception): await call('post_agent_message',message='must fail')
    async def test_http_guard(self):
        transport=httpx.ASGITransport(app=mcp_bridge.app)
        async with httpx.AsyncClient(transport=transport,base_url='http://127.0.0.1:8765') as client:
            r=await client.post('/mcp',content=b'x'*8193)
            self.assertEqual(r.status_code,413)
            r=await client.post('/mcp',headers={'Origin':'https://evil.invalid'},content=b'{}')
            self.assertEqual(r.status_code,403)
            r=await client.post('/mcp',headers={'Host':'evil.invalid'},content=b'{}')
            self.assertEqual(r.status_code,403)

if __name__=='__main__': unittest.main()
