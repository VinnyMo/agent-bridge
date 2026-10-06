"""One-time private upgrade snapshot under the service's existing permissions.

Called while holding the message-log lock, after index/identity commits. SQLite
uses bounded online backups, never ordinary copies of live database files.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time


def upgrade_backup(board, fd):
    destination = board.log.parent / 'backup-before-budget-v1'
    deadline = time.monotonic() + 10
    if destination.exists():
        manifest = json.loads((destination / 'manifest.json').read_text())
        if manifest.get('reference_protocol') != 'budget-v1' or manifest.get('verified') is not True:
            raise OSError('Upgrade backup is not verified')
        expected = {'messages.jsonl', board.db_path.name, board.identity_path.name, 'message-redactions.json'}
        if set(manifest.get('sha256', {})) != expected:
            raise OSError('Upgrade backup manifest incomplete')
        for name, expected_hash in manifest['sha256'].items():
            digest = hashlib.sha256()
            with (destination / name).open('rb') as f:
                while chunk := f.read(1024 * 1024):
                    if time.monotonic() > deadline: raise OSError('Backup verification deadline exceeded')
                    digest.update(chunk)
            if digest.hexdigest() != expected_hash: raise OSError('Upgrade backup checksum mismatch')
        return
    staged = Path(tempfile.mkdtemp(prefix='.backup-budget-v1-', dir=board.log.parent))
    try:
        def check(*_):
            if time.monotonic() > deadline: raise OSError('Upgrade backup deadline exceeded')

        # The log lock freezes coordinated appends throughout this snapshot.
        with os.fdopen(os.dup(fd), 'rb') as source, (staged / 'messages.jsonl').open('wb') as out:
            source.seek(0)
            while chunk := source.read(1024 * 1024):
                check(); out.write(chunk)
            out.flush(); os.fsync(out.fileno())
        for source in (board.db_path, board.identity_path):
            with contextlib.closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as live:
                with contextlib.closing(sqlite3.connect(staged / source.name)) as copy:
                    live.backup(copy, pages=128, progress=check, sleep=0.01)
                    if copy.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise OSError('Upgrade backup database verification failed')
        (staged / 'message-redactions.json').write_bytes(board.redactions.read_bytes())
        # Check restored source identities and references, without exposing any
        # bodies or private accounting in a receipt or public API.
        with contextlib.closing(sqlite3.connect((staged / board.identity_path.name).as_uri() + '?mode=ro', uri=True)) as identities:
            with contextlib.closing(sqlite3.connect((staged / board.db_path.name).as_uri() + '?mode=ro', uri=True)) as index:
                count = 0
                with (staged / 'messages.jsonl').open() as log:
                    for line in log:
                        check()
                        item = json.loads(line)
                        row = identities.execute('SELECT seq,created_at,refs FROM identities WHERE id=?', (item['id'],)).fetchone()
                        if row is None or row[1] != item['created_at'] or json.loads(row[2]) != item.get('references', []):
                            raise OSError('Upgrade backup identity verification failed')
                        if not index.execute('SELECT 1 FROM posts WHERE id=? AND seq=?', (item['id'], row[0])).fetchone():
                            raise OSError('Upgrade backup index verification failed')
                        count += 1
                if count != index.execute('SELECT COUNT(*) FROM posts').fetchone()[0]:
                    raise OSError('Upgrade backup log verification failed')
        files = {}
        for path in staged.iterdir():
            digest = hashlib.sha256()
            with path.open('rb') as f:
                while chunk := f.read(1024 * 1024): check(); digest.update(chunk)
                os.fsync(f.fileno())
            path.chmod(0o600)
            files[path.name] = digest.hexdigest()
        manifest = dict(reference_protocol='budget-v1', verified=True, available_posts=count, sha256=files)
        with (staged / 'manifest.json').open('w') as f:
            json.dump(manifest, f); f.write('\n'); f.flush(); os.fsync(f.fileno())
        directory = os.open(staged, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
        os.rename(staged, destination)
        directory = os.open(board.log.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if staged.exists(): shutil.rmtree(staged)
