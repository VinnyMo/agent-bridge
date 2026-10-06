import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from board import Board, Error


class ReferenceGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.redactions = self.root / 'redactions.json'
        self.redactions.write_text('{}')
        self.board = Board(self.root / 'messages.jsonl', self.redactions)
        # Seed a historical fixture in one write; exercise recovery, then use
        # ordinary appends. No quota or fsync mocks, no public board traffic.
        records = [dict(id=f'{n:024x}', sequence=n, created_at='2026-10-01T00:00:00Z',
                        agent='Fixture', message=f'original {n}', references=[])
                   for n in range(1, 121)]
        self.board.log.write_text(''.join(json.dumps(r) + '\n' for r in records))

    def post(self, message='reply', references=None, **extra):
        return self.board.append('/api/messages', dict(message=message, references=references or [], **extra), 'fixture')

    def graph(self, view, **params):
        return self.board.get('/api/graph/v1/' + view, params)

    def test_many_distinct_references_survive_rebuild_and_paginate(self):
        original = self.board.log.read_bytes()
        receipt = self.post(references=list(range(120, 0, -1)))
        self.assertTrue(self.board.log.read_bytes().startswith(original))
        self.board.db_path.unlink()
        post = self.board.get('/api/messages/121', {})['message']
        self.assertEqual(post['references'], list(range(120, 0, -1)))
        block = post['outgoing_references']
        self.assertEqual((block['total_count'], block['returned_count'], block['omitted_count']), (120, 8, 112))
        self.assertFalse(block['complete'])
        cursor = 0
        numbers = []
        while True:
            page = self.board.get('/api/messages/121/references', dict(direction='outgoing', after=cursor, limit=17, snapshot=121))
            numbers.extend(p['sequence'] for p in page['messages'])
            cursor = page['next_cursor']
            self.assertEqual(page['total_count'], page['previous_count'] + page['returned_count'] + page['omitted_count'])
            if not page['has_more']: break
        self.assertEqual(numbers, list(range(1, 121)))
        self.assertTrue(page['complete'])
        self.assertEqual(receipt['sequence'], 121)

    def test_shared_budget_duplicates_and_legacy_grandfathering(self):
        # Legacy posts near the former message limit remain valid on rebuild.
        record = dict(id=f'{121:024x}', sequence=121, created_at='2026-10-01T00:00:00Z',
                      message='x' * 7000, references=list(range(1, 9)))
        with self.board.log.open('a') as f: f.write(json.dumps(record) + '\n')
        before = self.board.log.read_bytes()
        self.assertEqual(len(self.board.get('/api/messages/121', {})['message']['message']), 7000)
        for payload in [dict(message='x'*7000, references=[1]), dict(message='x', references=[1, 1]),
                        dict(message='x', references=[True]), dict(message='x', references=['1']),
                        dict(message='x', references=list(range(1, 2000)))]:
            with self.assertRaises(Error): self.board.append('/api/messages', payload, 'fixture')
        self.assertEqual(self.board.log.read_bytes(), before)
        self.post(message='x'*6997, references=[1])  # exactly 7,000 combined bytes
        self.assertEqual(self.board.get('/api/status', {})['limits']['reference_count_limit'], None)

    def test_preview_has_no_append_quota_or_identity_effect(self):
        self.board.limit = 0
        before = self.board.log.read_bytes()
        result = self.board.preview(dict(message='Consider #1, #1 and the example #2. Not #999.', references=[1]))
        self.assertEqual(result['reference_warnings']['unlinked_numbers'], [2])
        self.assertFalse(result['appended'])
        self.assertEqual(self.board.log.read_bytes(), before)
        self.assertEqual(self.board.get('/api/status', {})['latest_sequence'], 120)
        self.board.limit = 999
        receipt = self.post(message='Consider #2', references=[1])
        self.assertEqual(receipt['reference_warnings']['unlinked_numbers'], [2])
        self.assertEqual(self.board.get('/api/messages/121', {})['message']['agent'], 'Unlabeled')

    def test_recent_window_is_bounded_and_discloses_earlier_history(self):
        page = self.board.get('/api/messages/recent', {})
        self.assertEqual([p['sequence'] for p in page['messages']], list(range(96, 121)))
        self.assertEqual(page['earlier_count'], 95)
        self.assertFalse(page['history_complete'])
        self.assertTrue(page['complete'])  # no omissions after the supplied window cursor
        self.assertEqual(len(self.board.get('/messages.json', {})['messages']), 120)
        first = self.board.get('/api/messages/recent', dict(after=0, limit=5, snapshot=120))
        self.post()
        second = self.board.get('/api/messages/recent', dict(after=first['next_cursor'], limit=5, snapshot=120))
        self.assertEqual([p['sequence'] for p in second['messages']], list(range(6, 11)))

    def test_graph_paging_incremental_replay_and_distinct_provenance(self):
        self.post(message='Example #2, #2; #1 and unknown #999.', references=[1])
        status = self.graph('status')
        common = dict(snapshot=121, public_revision=status['public_revision'])
        recorded = self.graph('edges', **common)
        self.assertEqual([(e['source'], e['target'], e['kind']) for e in recorded['edges']], [(121, 1, 'recorded_reference')])
        detected = self.graph('edges', kind='detected_mention', **common)
        self.assertEqual([e['target'] for e in detected['edges']], [1, 2])
        cursor = 0; nodes = []
        while True:
            page = self.graph('nodes', after=cursor, limit=19, **common)
            nodes.extend(page['nodes']); cursor = page['next_cursor']
            if not page['has_more']: break
        self.assertEqual([p['sequence'] for p in nodes], list(range(1, 122)))
        self.assertTrue(all('message' not in p and 'agent' not in p for p in nodes))
        self.post(message='appended later', references=[2, 3])
        self.assertEqual(self.graph('status')['public_revision'], status['public_revision'])
        self.assertEqual(self.graph('edges', **common), recorded | {'latest_sequence': 122})
        replay = recorded['edges'] + self.graph('edges', after=recorded['next_cursor'], snapshot=122, public_revision=status['public_revision'])['edges']
        self.assertEqual(replay, self.graph('edges', snapshot=122)['edges'])

    def test_redaction_invalidates_even_without_appends_and_removes_mentions(self):
        r = self.post(message='Private-context fixture mentions #1', references=[2])
        old = self.graph('status')
        self.assertEqual(len(self.graph('edges', kind='detected_mention')['edges']), 1)
        self.redactions.write_text(json.dumps({r['id']: 'Public replacement.'}))
        current = self.graph('status')
        self.assertEqual(current['snapshot'], old['snapshot'])
        self.assertNotEqual(current['public_revision'], old['public_revision'])
        for view in ('status', 'nodes', 'edges'):
            with self.assertRaises(Error) as e: self.graph(view, public_revision=old['public_revision'])
            self.assertEqual(e.exception.status, 409)
        self.assertEqual(self.graph('edges', kind='detected_mention')['edges'], [])
        self.assertEqual(self.graph('edges')['edges'][0]['target'], 2)

    def test_tombstones_and_scan_limits_keep_graph_partial(self):
        self.post(message='mentions #1', references=[1])
        old = self.graph('status')
        lines = self.board.log.read_bytes().splitlines(keepends=True)
        self.board.log.write_bytes(b''.join(lines[1:]))
        page = self.graph('nodes')
        self.assertEqual(page['nodes'][0]['availability'], 'unavailable')
        self.assertFalse(page['complete'])
        self.assertNotEqual(page['public_revision'], old['public_revision'])
        self.assertEqual(self.graph('edges')['edges'][0]['target_availability'], 'unavailable')
        detected = self.graph('edges', kind='detected_mention')
        self.assertEqual(detected['unknown_source_count'], 1)
        self.assertFalse(detected['complete'])

    def test_upgrade_backup_can_restore_and_failure_prevents_new_writes(self):
        self.board.backup_pending = True
        first = self.post(references=list(range(1, 30)))
        backup = self.root / 'backup-before-budget-v1'
        manifest = json.loads((backup / 'manifest.json').read_text())
        self.assertTrue(manifest['verified'])
        for name, digest in manifest['sha256'].items():
            self.assertEqual(hashlib.sha256((backup / name).read_bytes()).hexdigest(), digest)
        # Restore into a separate temporary location, never onto live files.
        import shutil
        restored = self.root / 'restored'; restored.mkdir()
        for name in manifest['sha256']: shutil.copyfile(backup / name, restored / name)
        recovered = Board(restored / 'messages.jsonl', restored / 'message-redactions.json')
        self.assertEqual(recovered.get('/api/status', {})['total_posts'], 120)
        self.assertEqual(recovered.get('/api/messages/1', {})['message']['message'], 'original 1')
        self.board.backup_pending = True
        self.post()  # verified backup is reused, not replaced by post-upgrade data
        self.assertEqual(json.loads((backup / 'manifest.json').read_text()), manifest)
        (backup / 'manifest.json').write_text('{}')
        self.board.backup_pending = True
        before = self.board.log.read_bytes()
        with self.assertRaises(Error): self.post()
        self.assertEqual(self.board.log.read_bytes(), before)

    def test_graph_preview_schema_and_legacy_detection_limits(self):
        import jsonschema
        schema=json.loads((Path(__file__).resolve().parent.parent / 'openapi.yaml').read_text())
        # OpenAPI 3.0 nullable becomes the equivalent JSON Schema union.
        def nullable(value):
            if isinstance(value,dict):
                if value.get('nullable') and 'type' in value: value['type']=[value['type'],'null']
                for child in value.values(): nullable(child)
            elif isinstance(value,list):
                for child in value: nullable(child)
        nullable(schema)
        self.post(message='Consider #1',references=[2])
        for name,value in [('GraphStatus',self.graph('status')),('GraphNodes',self.graph('nodes')),
                           ('GraphEdges',self.graph('edges')),('PostPreview',self.board.preview({'message':'#1'}))]:
            jsonschema.validate(value, {'$ref':'#/components/schemas/'+name,'components':schema['components']})
        record=dict(id=f'{122:024x}',sequence=122,created_at='2026-10-01T00:00:00Z',message='x'*7001+' #3',references=[])
        # This legacy body exceeds the mention scan bound.
        with self.board.log.open('a') as f: f.write(json.dumps(record)+'\n')
        detected=self.graph('edges',kind='detected_mention')
        self.assertEqual(detected['scan_limited_source_count'],1)
        self.assertFalse(detected['complete'])
        self.assertFalse(any(e['source']==122 for e in detected['edges']))

    def test_edge_cursor_can_resume_inside_large_reference_list(self):
        self.post(references=list(range(1, 121)))
        first = self.graph('edges', limit=13)
        self.assertEqual(first['next_cursor'], '121:13')
        second = self.graph('edges', after=first['next_cursor'], limit=100)
        third = self.graph('edges', after=second['next_cursor'])
        self.assertEqual([e['target'] for p in (first, second, third) for e in p['edges']], list(range(1, 121)))
        self.assertTrue(third['complete'])
        for params in ({'after': '999:1'}, {'after': '1:1'}, {'after': 'bad'}, {'kind': 'supports'}, {'limit': 0}):
            with self.assertRaises(Error): self.graph('edges', **params)


if __name__ == '__main__': unittest.main()
