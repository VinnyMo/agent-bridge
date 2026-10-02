import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from board import Board, Error

class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.redactions=self.root/'redactions.json'; self.redactions.write_text('{}\n')
        self.lore=self.root/'lore.json'; self.lore.write_text('[]')
        self.log=self.root/'messages.jsonl'
        self.board=Board(self.log,self.redactions,self.lore)
    def tearDown(self): self.tmp.cleanup()
    def post(self,msg='hello',**kwargs):
        return self.board.append('/api/messages',{'agent':'Test','message':msg,**kwargs},'test-ip')
    def get(self,**params): return self.board.get('/api/messages',params)
    def test_legacy_history_and_recovery(self):
        old={'id':'old','created_at':'2026-09-28T00:00:00+00:00','agent':'old','message':'kept','ip_hash':'hidden','utc_day':'2026-09-28'}
        self.log.write_text(json.dumps(old)+'\n'); original=self.log.read_bytes()
        self.assertEqual(self.get()['messages'][0]['sequence'],1)
        self.assertEqual(self.post()['sequence'],2)
        self.assertTrue(self.log.read_bytes().startswith(original))
        self.board.db_path.unlink()
        self.assertEqual([m['sequence'] for m in self.get()['messages']],[1,2])
        self.assertNotIn('ip_hash',json.dumps(self.get()))
    def test_concurrency_and_rebuild(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda n:self.post(str(n)),range(40)))
        self.assertEqual(sorted(r['sequence'] for r in results),list(range(1,41)))
        self.board.db_path.unlink()
        self.assertEqual(self.post()['sequence'],41)
    def test_snapshot_cursor_and_response_budget(self):
        for _ in range(22): self.post('x'*7000)
        page=self.get(limit=100)
        self.assertTrue(page['has_more']); self.assertLess(len(page['messages']),22)
        self.post('new outside snapshot')
        second=self.get(after=page['next_cursor'],snapshot=page['snapshot'])
        self.assertEqual(second['messages'][-1]['sequence'],22)
        self.assertFalse(second['has_more'])
        self.assertEqual(second['latest_sequence'],23)
        self.assertEqual(len(page['messages'])+len(second['messages']),22)
        with self.assertRaises(Error): self.get(after=24)
    def test_redacted_search_lookup_and_references(self):
        a=self.post('Private NEEDLE'); self.post('alpha BETA', references=[1]); self.post('beta alpha')
        self.redactions.write_text(json.dumps({a['id']:'Public replacement'}))
        self.assertEqual(self.get(query='needle')['matching_count'],0)
        self.assertEqual(self.get(query='ALPHA beta')['matching_count'],1)
        self.assertEqual(self.get(query='ALPHA beta',mode='keywords')['matching_count'],2)
        self.assertEqual(self.get(references=1)['messages'][0]['sequence'],2)
        self.assertTrue(self.board.get('/api/messages/1',{})['message']['redacted'])
        with self.assertRaises(Error): self.post(references=[99])
        with self.assertRaises(Error): self.post(references=[1,1])
        with self.assertRaises(Error): self.post(references=[True])
        self.redactions.write_text('{')
        with self.assertRaises(Error): self.get()
    def test_999_quota_and_long_posts(self):
        for _ in range(999): self.post('message longer than old limit '+'x'*450)
        with self.assertRaises(Error) as caught: self.post()
        self.assertEqual(caught.exception.status,429)
        self.board.append('/api/messages',{'message':'other IP'},'another-ip')
    def test_retired_preservation_rejects_reads_and_writes(self):
        self.post('Ordinary post remains available')
        with self.assertRaises(Error) as caught:
            self.board.get('/api/preservation/batches', {})
        self.assertEqual(caught.exception.status, 410)
        with self.assertRaises(Error) as caught:
            self.board.append('/api/preservation/batches/batch-1/volunteers', {'agent': 'Test'}, 'test-ip')
        self.assertEqual(caught.exception.status, 410)
        self.assertEqual(len(self.get()['messages']), 1)
    def test_retired_lore_returns_gone(self):
        with self.assertRaises(Error) as caught:
            self.board.get('/api/lore', {})
        self.assertEqual(caught.exception.status, 410)
    def test_limits_and_capacity_preserve_bytes(self):
        for msg in ('',' '*5,'x'*7001,'😀'*584,'bad\x00','bad\ud800'):
            with self.assertRaises(Error): self.post(msg)
        self.post('x'*7000)
        original=self.log.read_bytes(); self.board.max_log=len(original)
        with self.assertRaises(Error) as caught: self.post()
        self.assertEqual(caught.exception.status,507)
        self.assertEqual(original,self.log.read_bytes())
    def test_partial_record_fails_closed(self):
        self.post()
        with self.log.open('ab') as out: out.write(b'{"message":')
        with self.assertRaises(Error): self.post()
    def test_recovery_after_append_before_index_commit(self):
        self.post()
        record=dict(id='crash-recovery',sequence=2,message='durable',agent='Test',created_at=datetime.now(timezone.utc).isoformat(),utc_day=datetime.now(timezone.utc).date().isoformat(),ip_hash='test-ip')
        with self.log.open('a') as out: out.write(json.dumps(record)+'\n')
        self.assertEqual(self.get()['latest_sequence'],2)
        self.assertEqual(self.post()['sequence'],3)

if __name__=='__main__': unittest.main()
