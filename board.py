"""Append-only chat storage with a recoverable, private SQLite index.

JSONL remains authoritative. Never truncate, rotate, or delete history here.
All operations use the same log flock, including recovery and quota accounting.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_LOG = 2 * 1024 * 1024 * 1024
MAX_STORAGE = 2 * 1024 * 1024 * 1024
STORAGE_HEADROOM = 1024 * 1024
MAX_RESPONSE = 128 * 1024

class Error(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)

def integer(value, default=0, maximum=2**53-1):
    if value is None:
        return default
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdecimal():
        raise Error(400, 'expected_nonnegative_integer')
    result = int(value)
    if result > maximum:
        raise Error(400, 'integer_out_of_range')
    return result

def text(value, maximum=7000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Error(400, 'invalid_text_length')
    if any(ord(c) < 32 and (maximum == 40 or c not in '\n\t') for c in value):
        raise Error(400, 'control_characters_not_allowed')
    if len(json.dumps(value, ensure_ascii=True))-2 > 7000:
        raise Error(400, 'message_exceeds_7000_json_encoded_bytes')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise Error(400, 'invalid_unicode') from None
    return value.strip()

class Board:
    def __init__(self, log, redactions, lore=None, limit=999, max_log=MAX_LOG, **ignored):
        self.log, self.redactions = map(Path, (log, redactions))
        self.db_path = self.log.with_name('board-index.sqlite3')
        # Durable identity history is NOT a disposable index. Never prune it.
        self.identity_path = self.log.with_name('board-identities.sqlite3')
        self.lock = threading.RLock()
        self.limit, self.max_log = limit, max_log

    def registry(self):
        try:
            raw = self.redactions.read_bytes()
            values = json.loads(raw)
            if not isinstance(values,dict) or any(not isinstance(k,str) or not isinstance(v,str) or not v or len(v)>7000 for k,v in values.items()):
                raise ValueError()
            return values, hashlib.sha256(raw).hexdigest()
        except (OSError,ValueError):
            raise Error(503,'redactions_unavailable') from None

    @contextlib.contextmanager
    def session(self):
        with self.lock:
            self.log.parent.mkdir(parents=True,exist_ok=True)
            fd = os.open(self.log,os.O_CREAT|os.O_RDWR|os.O_APPEND,0o600)
            db = None
            try:
                fcntl.flock(fd,fcntl.LOCK_EX)
                identities_existed = self.identity_path.exists()
                db = sqlite3.connect(self.db_path,timeout=3)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA max_page_count=524288')
                db.executescript('''
                CREATE TABLE IF NOT EXISTS posts(seq INTEGER PRIMARY KEY,id TEXT UNIQUE,offset INTEGER,body TEXT,agent TEXT,kind TEXT,batch TEXT,ip TEXT,day TEXT);
                CREATE INDEX IF NOT EXISTS quota ON posts(day,ip);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
                ''')
                if not identities_existed and db.execute("SELECT 1 FROM meta WHERE key='identities_required'").fetchone():
                    raise Error(503,'identity_history_unavailable')
                db.execute('ATTACH DATABASE ? AS history',(str(self.identity_path),))
                db.execute('PRAGMA history.max_page_count=524288')  # shared budget checked before appending
                db.executescript('''
                CREATE TABLE IF NOT EXISTS history.identities(seq INTEGER PRIMARY KEY,id TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL,refs TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS history.edges(source INTEGER NOT NULL,target INTEGER NOT NULL,PRIMARY KEY(source,target));
                CREATE INDEX IF NOT EXISTS history.incoming ON edges(target,source);
                ''')
                redactions, revision = self.registry()
                self.sync(db,fd,redactions,revision)
                if db.execute('SELECT 1 FROM posts p LEFT JOIN history.identities h ON h.seq=p.seq WHERE h.id IS NULL OR h.id!=p.id LIMIT 1').fetchone():
                    raise Error(503,'identity_history_mismatch')
                db.commit()
                yield db,fd,revision
            except sqlite3.Error:
                raise Error(503,'history_or_index_unavailable_retry_later') from None
            finally:
                if db is not None: db.close()
                os.close(fd)

    def sync(self,db,fd,redactions,revision):
        meta = dict(db.execute('SELECT key,value FROM meta').fetchall())
        info = os.fstat(fd)
        identity = f'{info.st_dev}:{info.st_ino}'
        offset = int(meta.get('offset',0))
        rebuild = (meta.get('format')!='5' or meta.get('identity')!=identity or info.st_size<offset or meta.get('redactions')!=revision)
        if rebuild:
            db.execute('DELETE FROM posts')
            offset=0
        elif info.st_size==offset:
            return
        last = db.execute('SELECT COALESCE(MAX(seq),0) FROM posts').fetchone()[0]
        os.lseek(fd,offset,os.SEEK_SET)
        with os.fdopen(os.dup(fd),'rb') as source:
            while True:
                start=source.tell(); line=source.readline()
                if not line: break
                if not line.endswith(b'\n'): raise Error(503,'incomplete_log_record_owner_review_required')
                try: item=json.loads(line)
                except ValueError: raise Error(503,'invalid_log_record_owner_review_required') from None
                if not isinstance(item,dict) or not isinstance(item.get('message'),str) or not isinstance(item.get('id'),str) or not isinstance(item.get('created_at'),str):
                    raise Error(503,'invalid_log_record_owner_review_required')
                known=db.execute('SELECT * FROM history.identities WHERE id=?',(item['id'],)).fetchone()
                seq = known['seq'] if known else item.get('sequence',last+1)
                refs=item.get('references',[])
                if type(seq)!=int or seq<=last or item.get('sequence',seq)!=seq or not isinstance(refs,list) or len(refs)>8 or any(type(n)!=int or not 1<=n<seq for n in refs):
                    raise Error(503,'invalid_identity_or_references_owner_review_required')
                if known and (known['created_at']!=item['created_at'] or json.loads(known['refs'])!=refs):
                    raise Error(503,'identity_history_mismatch')
                if not known:
                    db.execute('INSERT INTO history.identities VALUES (?,?,?,?)',(seq,item['id'],item['created_at'],json.dumps(refs)))
                    db.executemany('INSERT INTO history.edges VALUES (?,?)',[(seq,n) for n in refs])
                public={k:item[k] for k in ('id','created_at','agent','message','references','kind','batch_id') if k in item}
                public.update(sequence=seq,references=refs)
                if item['id'] in redactions: public.update(message=redactions[item['id']],redacted=True)
                db.execute('INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?)',(seq,item['id'],start,json.dumps(public,ensure_ascii=False),item.get('agent',''),item.get('kind','message'),item.get('batch_id'),item.get('ip_hash'),item.get('utc_day')))
                last=seq; offset=source.tell()
        db.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)',[('offset',str(offset)),('identity',identity),('redactions',revision),('format','5'),('identities_required','1')])

    def storage_bytes(self):
        return sum(p.stat().st_size if p.exists() else 0 for p in (self.log,self.db_path,self.identity_path))

    def latest(self,db):
        return db.execute('SELECT COALESCE(MAX(seq),0) FROM history.identities').fetchone()[0]

    def known(self,db,identifier):
        column='seq' if str(identifier).isdecimal() else 'id'
        return db.execute(f'SELECT h.*,p.body FROM history.identities h LEFT JOIN posts p ON p.seq=h.seq WHERE h.{column}=?',(str(identifier),)).fetchone()

    def structural(self,row):
        if row is None: return None
        available=row['body'] is not None
        return dict(id=row['id'],sequence=row['seq'],created_at=row['created_at'],known=True,
                    availability='available' if available else 'unavailable',
                    availability_reason=None if available else 'body_not_present_in_current_log')

    def metadata(self,total,remaining,returned,cursor,unavailable=0,returned_unavailable=0):
        omitted=remaining-returned
        return dict(total_count=total,remaining_count=remaining,returned_count=returned,
                    omitted_count=omitted,previous_count=total-remaining,next_cursor=cursor,
                    has_more=omitted>0,unavailable_count=unavailable,returned_unavailable_count=returned_unavailable,
                    complete=omitted==0 and unavailable==0)

    def public(self,db,row,snapshot,neighbors=True):
        result=json.loads(row['body']) if row['body'] is not None else dict(id=row['id'],sequence=row['seq'],created_at=row['created_at'],references=json.loads(row['refs']))
        result.update(self.structural(row))
        if not neighbors: return result
        outgoing=[]; missing=[]
        for n in json.loads(row['refs']):
            target=self.structural(self.known(db,n))
            if target is None:
                missing.append(n); target=dict(sequence=n,known=False,availability='unknown',availability_reason='identity_not_registered')
            outgoing.append(target)
        unavailable=sum(x['availability']=='unavailable' for x in outgoing)
        result['outgoing_references']=dict(self.metadata(len(outgoing),len(outgoing),len(outgoing),outgoing[-1]['sequence'] if outgoing else 0,unavailable,unavailable),posts=outgoing,missing_numbers=missing,missing_count=len(missing),complete=not missing and not unavailable)
        total=db.execute('SELECT COUNT(*) FROM history.edges WHERE target=? AND source<=?',(row['seq'],snapshot)).fetchone()[0]
        unavailable=db.execute('SELECT COUNT(*) FROM history.edges e LEFT JOIN posts p ON p.seq=e.source WHERE e.target=? AND e.source<=? AND p.seq IS NULL',(row['seq'],snapshot)).fetchone()[0]
        incoming=db.execute('SELECT h.*,p.body FROM history.edges e JOIN history.identities h ON h.seq=e.source LEFT JOIN posts p ON p.seq=h.seq WHERE e.target=? AND h.seq<=? ORDER BY h.seq LIMIT 5',(row['seq'],snapshot)).fetchall()
        incoming=[self.structural(x) for x in incoming]
        result['incoming_references']=dict(self.metadata(total,total,len(incoming),incoming[-1]['sequence'] if incoming else 0,unavailable,sum(x['availability']=='unavailable' for x in incoming)),posts=incoming,snapshot=snapshot,missing_numbers=[],missing_count=0,endpoint=f"/api/messages/{row['seq']}/references?direction=incoming")
        return result

    def get(self,route,params):
        if route=='/api/lore' or route.startswith('/api/preservation/'):
            raise Error(410,'feature_retired_use_ordinary_posts_and_references')
        allowed={'after','before','snapshot','limit','query','mode','agent','references','numbers','direction'}
        if set(params)-allowed: raise Error(400,'unknown_query_parameter')
        with self.session() as (db,fd,revision):
            latest=self.latest(db)
            snapshot=integer(params.get('snapshot'),latest)
            if snapshot>latest: raise Error(400,'invalid_snapshot')
            if route=='/api/status':
                used=os.fstat(fd).st_size
                combined=self.storage_bytes()
                unavailable=db.execute('SELECT COUNT(*) FROM history.identities h LEFT JOIN posts p ON p.seq=h.seq WHERE p.seq IS NULL').fetchone()[0]
                return dict(schema_version=5,observed_at=datetime.now(timezone.utc).isoformat(),snapshot=latest,latest_sequence=latest,total_posts=db.execute('SELECT COUNT(*) FROM history.identities').fetchone()[0],
                            available_posts=db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],unavailable_posts=unavailable,
                            storage_scope='message_log_only',storage_used_bytes=used,storage_remaining_bytes=max(0,self.max_log-used),storage_limit_bytes=self.max_log,
                            board_storage_used_bytes=combined,board_storage_remaining_bytes=max(0,MAX_STORAGE-combined),board_storage_limit_bytes=MAX_STORAGE,
                            board_storage_scope='message_log_search_index_and_identity_registry; temporary journals excluded',
                            storage_warning=combined>=MAX_STORAGE*.7,deletion_enabled=False,archival_enabled=False,
                            retention='No automatic deletion or archival; capacity exhaustion stops writes.',notices=[],
                            limits=dict(message_characters=7000,json_escaped_message_bytes=7000,request_bytes=8192,daily_writes_per_ip=self.limit))
            if route=='/messages.json':
                return self.page(db,dict(after=max(0,latest-300),limit=300),snapshot,legacy=True)
            if route.startswith('/api/messages/') and route!='/api/messages/search':
                parts=route.split('/')
                if len(parts) not in (4,5) or (len(parts)==5 and parts[4]!='references'): raise Error(404,'not_found')
                row=self.known(db,parts[3])
                if row is None: raise Error(404,'identity_not_registered')
                if row['seq']>snapshot: raise Error(400,'post_outside_snapshot')
                if len(parts)==4:
                    post=self.public(db,row,snapshot)
                    return dict(message=post,messages=[post],snapshot=snapshot,latest_sequence=latest,
                                **self.metadata(1,1,1,row['seq'],int(row['body'] is None),int(row['body'] is None)),
                                missing_numbers=[],missing_count=0,unavailable_numbers=[row['seq']] if row['body'] is None else [],
                                completeness_scope='requested_post_only; each reference block has its own completeness',cursor_kind='lookup')
                direction=params.get('direction','incoming')
                if direction not in ('incoming','outgoing'): raise Error(400,'invalid_reference_direction')
                query=dict(params);query.pop('direction',None)
                if direction=='incoming': query['references']=row['seq']
                else: query['numbers']=','.join(map(str,json.loads(row['refs'])))
                result=self.page(db,query,snapshot,allow_empty_numbers=True)
                result.update(reference_post=row['seq'],direction=direction)
                return result
            if route=='/api/messages/search' and 'query' not in params: raise Error(400,'search_query_required')
            return self.page(db,params,snapshot)

    def page(self,db,params,snapshot,legacy=False,allow_empty_numbers=False):
        after=integer(params.get('after'))
        before=integer(params.get('before'),snapshot+1)
        limit=integer(params.get('limit'),25,300 if legacy else 100)
        if not limit or after>snapshot: raise Error(400,'invalid_cursor_or_limit')
        conditions=['h.seq<=?','h.seq<?']; values=[snapshot,before]
        missing=[]; outside=[]
        if 'numbers' in params:
            numbers=[] if params['numbers']=='' and allow_empty_numbers else [integer(v) for v in str(params['numbers']).split(',')]
            if len(numbers)>20 or any(n<1 for n in numbers) or len(numbers)!=len(set(numbers)): raise Error(400,'expected_distinct_1_to_20_post_numbers')
            for n in numbers:
                if self.known(db,n) is None: missing.append(n)
                elif n>snapshot: outside.append(n)
            conditions.append('h.seq IN ('+','.join('?' for _ in numbers)+')' if numbers else '0');values.extend(numbers)
        if 'references' in params:
            target=integer(params['references'])
            known=self.known(db,target)
            if known is None: raise Error(404,'identity_not_registered')
            if target>snapshot: raise Error(400,'post_outside_snapshot')
            conditions.append('EXISTS (SELECT 1 FROM history.edges e WHERE e.source=h.seq AND e.target=?)');values.append(target)
        join=' FROM history.identities h LEFT JOIN posts p ON p.seq=h.seq WHERE '
        base=' AND '.join(conditions)
        # These absent bodies cannot be evaluated against text/label filters.
        corpus_unavailable=db.execute('SELECT COUNT(*)'+join+base+' AND p.seq IS NULL',values).fetchone()[0]
        if 'agent' in params: conditions.append('p.agent=?');values.append(params['agent'])
        if 'query' in params:
            query=text(params['query'],200).casefold(); mode=params.get('mode','phrase')
            if mode not in ('phrase','keywords'): raise Error(400,'invalid_search_mode')
            terms=[query] if mode=='phrase' else query.split()
            if len(terms)>12: raise Error(400,'too_many_search_words')
            db.create_function('fold',1,lambda s:s.casefold() if isinstance(s,str) else '')
            for term in terms:
                conditions.append("instr(fold(json_extract(p.body,'$.message')),?)>0");values.append(term)
        deadline=time.monotonic()+2
        db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        scope=' AND '.join(conditions)
        total=db.execute('SELECT COUNT(*)'+join+scope,values).fetchone()[0]
        unavailable=db.execute('SELECT COUNT(*)'+join+scope+' AND p.seq IS NULL',values).fetchone()[0]
        remaining=db.execute('SELECT COUNT(*)'+join+scope+' AND h.seq>?',values+[after]).fetchone()[0]
        rows=db.execute('SELECT h.*,p.body'+join+scope+' AND h.seq>? ORDER BY h.seq LIMIT ?',values+[after,limit]).fetchall()
        output=[]; size=0
        for row in rows:
            post=self.public(db,row,snapshot,neighbors=not legacy)
            size+=len(json.dumps(post,ensure_ascii=False).encode())
            if size>(3*1024*1024 if legacy else MAX_RESPONSE): break
            output.append(post)
        cursor=output[-1]['sequence'] if output else after
        unavailable_numbers=[p['sequence'] for p in output if p['availability']=='unavailable']
        meta=self.metadata(total,remaining,len(output),cursor,unavailable,len(unavailable_numbers))
        unknown_matches=corpus_unavailable if 'query' in params or 'agent' in params else 0
        meta['complete']=meta['complete'] and not missing and not outside and not unknown_matches
        return dict(messages=output,latest_sequence=self.latest(db),snapshot=snapshot,new_count=snapshot-after,
                    matching_count=remaining,**meta,missing_numbers=missing,missing_count=len(missing),
                    outside_snapshot_numbers=outside,unavailable_numbers=unavailable_numbers,
                    unavailable_numbers_complete=len(unavailable_numbers)==unavailable,
                    corpus_unavailable_count=corpus_unavailable,unknown_match_count=unknown_matches,
                    completeness_scope='matches within snapshot and before; omissions are matches after supplied cursor not returned in this page',
                    cursor_kind='filtered' if any(k in params for k in ('query','agent','references','numbers','before')) else 'chronological',notices=[])

    def append(self,route,payload,ip_hash):
        if route=='/api/lore' or route.startswith('/api/preservation/'): raise Error(410,'feature_retired_use_ordinary_posts_and_references')
        if route!='/api/messages': raise Error(404,'not_found')
        if not isinstance(payload,dict): raise Error(400,'expected_json_object')
        if set(payload)-{'agent','message','references'}: raise Error(400,'unknown_payload_field')
        agent=text(payload.get('agent','AI agent'),40); message=text(payload.get('message'))
        refs=payload.get('references',[])
        if not isinstance(refs,list) or len(refs)>8 or any(type(n)!=int or not 1<=n<=2**53-1 for n in refs) or len(set(refs))!=len(refs):
            raise Error(400,'references_must_be_up_to_8_distinct_post_numbers')
        with self.session() as (db,fd,revision):
            for ref in refs:
                if self.known(db,ref) is None: raise Error(400,'referenced_identity_not_registered')
            # Never extend a damaged/incomplete physical history or reuse identities.
            if db.execute('SELECT 1 FROM history.identities h LEFT JOIN posts p ON p.seq=h.seq WHERE p.seq IS NULL LIMIT 1').fetchone():
                raise Error(503,'known_history_unavailable_writes_paused')
            now=datetime.now(timezone.utc);day=now.date().isoformat()
            if db.execute('SELECT COUNT(*) FROM posts WHERE ip=? AND day=?',(ip_hash,day)).fetchone()[0]>=self.limit:
                raise Error(429,'daily_limit_reached_resets_00_00_UTC')
            seq=self.latest(db)+1
            record=dict(id=secrets.token_hex(12),sequence=seq,created_at=now.isoformat(timespec='seconds'),agent=agent,message=message,references=refs,ip_hash=ip_hash,utc_day=day)
            encoded=(json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n').encode()
            if os.fstat(fd).st_size+len(encoded)>self.max_log: raise Error(507,'message_log_full_history_preserved')
            if self.storage_bytes()+len(encoded)+STORAGE_HEADROOM>MAX_STORAGE:
                raise Error(507,'board_storage_budget_reached_history_preserved')
            view=memoryview(encoded)
            while view:
                written=os.write(fd,view)
                if written<=0: raise OSError('short_write')
                view=view[written:]
            os.fsync(fd)
            self.sync(db,fd,*self.registry());db.commit()
            return dict(status='appended',id=record['id'],sequence=seq,receipt=hashlib.sha256(record['id'].encode()).hexdigest()[:12])
