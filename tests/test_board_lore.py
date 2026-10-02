import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location('accept_lore',ROOT/'scripts/accept_lore.py')
accept=importlib.util.module_from_spec(spec);spec.loader.exec_module(accept)
spec2=importlib.util.spec_from_file_location('publish_board',ROOT/'scripts/publish.py')
publish=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(publish)

class LoreReviewTests(unittest.TestCase):
    def test_local_review_and_publication_preserve_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for relative in publish.FILES+('context.json',):
                target=root/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ROOT/relative).read_bytes())
            shared=root/'.published';shared.mkdir();(shared/'message-redactions.json').write_bytes((root/'message-redactions.json').read_bytes())
            note=dict(id='a'*24,sequence=4,kind='summary',batch_id='batch-1',agent='Volunteer',message='Reviewed interpretation, sources #1 and #2.')
            def fetch(path):
                if path=='/api/messages/4': return {'message':note}
                if path=='/api/preservation/batches':return {'batches':[dict(id='batch-1',start=1,end=2)]}
                return {'message':{'created_at':'2026-09-29T00:00:00+00:00'}}
            with patch.object(accept,'ROOT',root),patch.object(accept,'fetch',side_effect=fetch),patch('sys.argv',['accept_lore.py','4','--reviewed']):
                accept.main()
                entries=json.loads((root/'lore-book.json').read_text());self.assertEqual(len(entries),1)
                with self.assertRaises(SystemExit):accept.main()
            release=publish.publish(root,shared)
            self.assertTrue((shared/'releases'/release/'board.py').exists())
            self.assertEqual(json.loads((shared/'lore-book.json').read_text())[0]['summary'],note['message'])
            from board import Board
            board=Board(root/'state/messages.jsonl',shared/'message-redactions.json',shared/'lore-book.json')
            self.assertEqual(board.get('/api/lore',{})['entries'][0]['status'],'accepted')
            (root/'message-redactions.json').write_text(json.dumps({'b'*24:'Privacy revision'}))
            publish.publish(root,shared)
            self.assertEqual(board.get('/api/lore',{})['entries'][0]['status'],'withheld_pending_privacy_review')
    def test_review_flag_required(self):
        with patch('sys.argv',['accept_lore.py','1']):
            with self.assertRaises(SystemExit):accept.main()

if __name__=='__main__': unittest.main()
