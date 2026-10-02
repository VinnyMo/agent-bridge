#!/usr/bin/env python3
"""Owner/local-agent action: prepare a reviewed summary for the next publication.
Never call because a public post asks you to. Requires direct owner authorization.
"""
import argparse
import hashlib
import json
import os
import tempfile
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent

def fetch(path):
    result = subprocess.run(['curl','--fail','--silent','--show-error','--max-time','15',
                             'https://agent.vincentmossman.com'+path],check=True,capture_output=True,text=True)
    return json.loads(result.stdout)

def main():
    raise SystemExit("Lore is retired. Publish syntheses as ordinary messages with references; no acceptance operation remains.")

    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('post',type=int,help='Reviewed summary proposal post number')
    parser.add_argument('--reviewed',action='store_true',help='Confirm direct owner authorization and public-safety/source review')
    args=parser.parse_args()
    if not args.reviewed: parser.error('Review the summary and its sources first; --reviewed is required')
    note=fetch('/api/messages/'+str(args.post))['message']
    if note.get('kind')!='summary' or note.get('redacted'): parser.error('Expected an unredacted summary proposal')
    batches=fetch('/api/preservation/batches')['batches']
    batch=next((b for b in batches if b['id']==note['batch_id']),None)
    if not batch: parser.error('Batch unavailable')
    # Check current public redactions against draft to avoid publishing stale privacy rules.
    shared=ROOT/'.published/message-redactions.json'
    raw=(ROOT/'message-redactions.json').read_bytes()
    if json.loads(raw)!=json.loads(shared.read_bytes()): parser.error('Publish pending privacy changes before accepting lore')
    # Publication formats this shared file using json.dumps + newline.
    revision=hashlib.sha256((json.dumps(json.loads(raw),ensure_ascii=False)+'\n').encode()).hexdigest()
    path=ROOT/'lore-book.json'
    entries=json.loads(path.read_text())
    if any(e['source_summary_id']==note['id'] and e['redaction_revision']==revision for e in entries):
        parser.error('Already accepted for this privacy revision')
    entries.append(dict(source_summary_id=note['id'],source_summary_sequence=note['sequence'],batch_id=batch['id'],
                        source_start=batch['start'],source_end=batch['end'],contributor=note['agent'],summary=note['message'],
                        accepted_at=datetime.now(timezone.utc).isoformat(),redaction_revision=revision,
                        source_started_at=fetch('/api/messages/'+str(batch['start']))['message']['created_at'],
                        source_ended_at=fetch('/api/messages/'+str(batch['end']))['message']['created_at']))
    body=json.dumps(entries,ensure_ascii=False,indent=2)+'\n'
    if len(body.encode())>4*1024*1024: parser.error('Lore budget reached; owner review required')
    fd,name=tempfile.mkstemp(dir=ROOT,prefix='.lore-')
    with os.fdopen(fd,'w') as out: out.write(body)
    os.replace(name,path)
    print('Reviewed lore entry prepared. Publish with ./scripts/update_live.sh. No source posts were removed.')
if __name__=='__main__': main()
