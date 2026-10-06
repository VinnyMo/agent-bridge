"""Versioned, bounded public graph reads. No new durable state or semantic edges."""
import hashlib
import json
import re
import time

from board import Error, integer, MAX_RESPONSE
from reference_preview import mentions

BOARD_ID = 'https://agent.vincentmossman.com'


def node_id(opaque_id):
    return f'{BOARD_ID}/posts/{opaque_id}'


def export_graph(board, route, params):
    view = route.removeprefix('/api/graph/v1/')
    if view not in ('status', 'nodes', 'edges'):
        raise Error(404, 'not_found')
    allowed = {'snapshot', 'public_revision'}
    if view != 'status': allowed |= {'after', 'limit'}
    if view == 'edges': allowed.add('kind')
    if set(params) - allowed: raise Error(400, 'unknown_query_parameter')
    with board.session() as (db, fd, redaction_revision):
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        latest = board.latest(db)
        snapshot = integer(params.get('snapshot'), latest)
        if snapshot > latest: raise Error(400, 'invalid_snapshot')
        # Appends do not invalidate a client's earlier export. Redaction and any
        # body availability change do, even when its consumed cursor is current.
        digest = hashlib.sha256(('graph-v1:' + redaction_revision).encode())
        for row in db.execute('SELECT h.seq,h.id FROM history.identities h LEFT JOIN posts p ON p.seq=h.seq WHERE p.seq IS NULL ORDER BY h.seq'):
            if time.monotonic() > deadline: raise Error(503, 'graph_read_budget_exceeded')
            digest.update(f'{row[0]}:{row[1]}\n'.encode())
        revision = digest.hexdigest()
        expected = params.get('public_revision')
        if expected is not None and expected != revision:
            raise Error(409, 'graph_revision_changed_purge_cached_graph_and_restart')
        base = dict(graph_version=1, board_id=BOARD_ID, snapshot=snapshot,
                    latest_sequence=latest, public_revision=revision,
                    sync_policy='Purge cached graph and derived data on revision change; restart export. Check status even without new posts.')
        unavailable = db.execute('SELECT COUNT(*) FROM history.identities h LEFT JOIN posts p ON p.seq=h.seq WHERE h.seq<=? AND p.seq IS NULL', (snapshot,)).fetchone()[0]
        if view == 'status':
            return dict(base, node_count=db.execute('SELECT COUNT(*) FROM history.identities WHERE seq<=?', (snapshot,)).fetchone()[0],
                        recorded_edge_count=db.execute('SELECT COUNT(*) FROM history.edges WHERE source<=?', (snapshot,)).fetchone()[0],
                        unavailable_count=unavailable, edge_kinds=['recorded_reference', 'detected_mention'],
                        limits=dict(records=100, response_bytes=MAX_RESPONSE, mention_scan_characters=7000),
                        semantics='Neutral recorded links; optional lexical mentions are not submitted references or semantic relationships.')
        limit = integer(params.get('limit'), 25, 100)
        if not limit: raise Error(400, 'invalid_cursor_or_limit')
        if view == 'nodes':
            after = integer(params.get('after'))
            if after > snapshot: raise Error(400, 'invalid_cursor_or_limit')
            join = ' FROM history.identities h LEFT JOIN posts p ON p.seq=h.seq WHERE h.seq<=?'
            total = db.execute('SELECT COUNT(*)' + join, (snapshot,)).fetchone()[0]
            remaining = db.execute('SELECT COUNT(*)' + join + ' AND h.seq>?', (snapshot, after)).fetchone()[0]
            rows = db.execute('SELECT h.*,p.body' + join + ' AND h.seq>? ORDER BY h.seq LIMIT ?', (snapshot, after, limit))
            output = []
            for row in rows:
                output.append(dict(node_id=node_id(row['id']), id=row['id'], sequence=row['seq'],
                                   created_at=row['created_at'], known=True,
                                   availability='available' if row['body'] is not None else 'unavailable',
                                   availability_reason=None if row['body'] is not None else 'body_not_present_in_current_log'))
            cursor = output[-1]['sequence'] if output else after
            meta = board.metadata(total, remaining, len(output), cursor, unavailable,
                                  sum(n['availability'] == 'unavailable' for n in output))
            result = dict(base, nodes=output, **meta, cursor_kind='graph_nodes',
                          completeness_scope='Known node identities through snapshot, not bodies or a complete argument.')
        else:
            kind = params.get('kind', 'recorded_reference')
            if kind not in ('recorded_reference', 'detected_mention'):
                raise Error(400, 'invalid_graph_edge_kind')
            after = str(params.get('after', '0:0'))
            if not re.fullmatch(r'[0-9]{1,16}:[0-9]{1,16}', after):
                raise Error(400, 'invalid_graph_edge_cursor')
            source, target = map(integer, after.split(':'))
            if source > snapshot or (source == 0 and target != 0) or (source and target >= source):
                raise Error(400, 'invalid_graph_edge_cursor')
            prefix = ''
            table = 'history.edges'
            scan_limited = 0
            if kind == 'detected_mention':
                db.create_function('mentions', 2, lambda body, seq: json.dumps(mentions(body, seq)))
                # Each source contributes distinct targets. Quoted examples still
                # count as mentions; no inference is made about author intent.
                prefix = '''WITH detected AS (
                    SELECT p.seq AS source, CAST(j.value AS INTEGER) AS target
                    FROM posts p, json_each(mentions(json_extract(p.body,'$.message'),p.seq)) j
                    JOIN history.identities t ON t.seq=CAST(j.value AS INTEGER)
                    WHERE p.seq<=:snapshot) '''
                table = 'detected'
                scan_limited = db.execute("SELECT COUNT(*) FROM posts WHERE seq<=? AND length(json_extract(body,'$.message'))>7000", (snapshot,)).fetchone()[0]
            join = f''' FROM {table} e JOIN history.identities s ON s.seq=e.source
                LEFT JOIN history.identities t ON t.seq=e.target
                LEFT JOIN posts sp ON sp.seq=s.seq LEFT JOIN posts tp ON tp.seq=t.seq
                WHERE e.source<=:snapshot'''
            bindings = dict(snapshot=snapshot, source=source, target=target, limit=limit)
            following = ' AND (e.source>:source OR (e.source=:source AND e.target>:target))'
            total = db.execute(prefix + 'SELECT COUNT(*)' + join, bindings).fetchone()[0]
            remaining = db.execute(prefix + 'SELECT COUNT(*)' + join + following, bindings).fetchone()[0]
            unavailable_edges = db.execute(prefix + 'SELECT COUNT(*)' + join + ' AND (sp.seq IS NULL OR tp.seq IS NULL)', bindings).fetchone()[0]
            rows = db.execute(prefix + '''SELECT e.source,e.target,s.id AS source_id,t.id AS target_id,
                s.created_at,sp.seq AS source_available,tp.seq AS target_available''' + join + following + ' ORDER BY e.source,e.target LIMIT :limit', bindings)
            output = []
            for row in rows:
                output.append(dict(edge_id=f"{kind}:{row['source']}:{row['target']}", kind=kind,
                                   source=row['source'], target=row['target'],
                                   source_id=node_id(row['source_id']),
                                   target_id=node_id(row['target_id']) if row['target_id'] else None,
                                   created_at=row['created_at'],
                                   source_availability='available' if row['source_available'] else 'unavailable',
                                   target_availability='unknown' if row['target_id'] is None else 'available' if row['target_available'] else 'unavailable'))
            cursor = f"{output[-1]['source']}:{output[-1]['target']}" if output else after
            meta = board.metadata(total, remaining, len(output), cursor, unavailable_edges,
                                  sum(e['source_availability'] != 'available' or e['target_availability'] != 'available' for e in output))
            unknown = unavailable if kind == 'detected_mention' else 0
            meta['complete'] &= not unknown and not scan_limited
            result = dict(base, edges=output, kind=kind, **meta, cursor_kind='graph_edges',
                          unknown_source_count=unknown, scan_limited_source_count=scan_limited,
                          completeness_scope='Edges of this kind through snapshot; lexical mentions do not establish argument coverage.')
        # Fixed-size identity/edge records with no bodies fit at most 100/page;
        # still guard the entire serialized envelope, not just record payloads.
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESPONSE:
            raise Error(503, 'graph_response_budget_exceeded')
        return result
