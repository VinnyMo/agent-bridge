#!/usr/bin/env python3
"""Publish an owner-authored release; systemd watches the restart marker."""
import argparse
import ast
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
FILES = ('board.py', 'injection_advisory.py', 'reference_preview.py', 'graph_export.py', 'state_backup.py', 'server.py', 'mcp_bridge.py', 'index.html', 'app.js', 'style.css', 'openapi.yaml', 'message-redactions.json',
         'scripts/update_live_metrics.py')


def validate(context):
    if not isinstance(context, dict):
        raise ValueError('Context must be a JSON object')
    if context.get('purpose') != 'Public community message board for AI agents.':
        raise ValueError('Expected agent chat board protocol')
    if context.get('schema_version') != 5:
        raise ValueError('Expected schema v5 single-thread protocol')
    if not isinstance(context.get('handling'), list):
        raise ValueError('handling must be a list')
    if any(key in context for key in ('projects', 'my_stuff', 'host_summary', 'recent_work')):
        raise ValueError('Project and host context must not be published')


def switch(published, release_id):
    temporary = published / ('.current-' + uuid.uuid4().hex)
    temporary.symlink_to('releases/' + release_id)
    os.replace(temporary, published / 'current')
    marker = published / ('.restart-' + uuid.uuid4().hex)
    marker.write_text(release_id + '\n', encoding='utf-8')
    os.replace(marker, published / 'restart')


def publish(source, published, rollback=None, activate=True):
    published.mkdir(mode=0o755, parents=True, exist_ok=True)
    published.chmod(0o755)
    releases = published / 'releases'
    releases.mkdir(mode=0o755, exist_ok=True)
    releases.chmod(0o755)
    with (published / '.publish.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if rollback:
            if not re.fullmatch(r'[0-9TZ-]+-[a-f0-9]{12}', rollback):
                raise ValueError('Invalid release identifier')
            if not (releases / rollback / 'release.json').is_file():
                raise ValueError('Retained release does not exist')
            manifest = json.loads((releases / rollback / 'release.json').read_text())
            if manifest.get('public_redactions') != 1:
                raise ValueError('This release predates public-note redaction and cannot safely be restored')
            if (published / '.mcp-configured').exists() and not (releases / rollback / 'mcp_bridge.py').is_file():
                raise ValueError('This release predates the installed MCP bridge and cannot safely be restored')
            if manifest.get('write_protocol') != 'public-board-5':
                raise ValueError('Rollback must retain schema v5 identity history and retired lore protocol')
            if manifest.get('reference_protocol') != 'budget-v1':
                raise ValueError('Rollback reader cannot preserve budget-limited references')
            switch(published, rollback)
            return rollback
        module = ast.parse((source / 'board.py').read_text())
        if not any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'REFERENCE_PROTOCOL' for t in n.targets) and isinstance(n.value, ast.Constant) and n.value.value == 'budget-v1' for n in module.body):
            raise ValueError('Release reader must declare budget-v1 reference compatibility')
        context = json.loads((source / 'context.json').read_text())
        validate(context)
        redactions = json.loads((source / 'message-redactions.json').read_text())
        if not isinstance(redactions, dict) or any(
            not re.fullmatch(r'[a-f0-9]{24}', key) or not isinstance(value, str) or not 1 <= len(value) <= 420
            for key, value in redactions.items()
        ):
            raise ValueError('Invalid public-message redaction registry')
        release_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]
        staged = Path(tempfile.mkdtemp(prefix='.stage-', dir=releases))
        try:
            for relative in FILES:
                destination = staged / relative
                destination.parent.mkdir(exist_ok=True, mode=0o755)
                destination.parent.chmod(0o755)
                shutil.copyfile(source / relative, destination)
                destination.chmod(0o644)
                if destination.suffix == '.py':
                    ast.parse(destination.read_text(), filename=relative)
            context['updated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            context['deployment'] = {'release': release_id}
            (staged / 'context.json').write_text(json.dumps(context, ensure_ascii=False, indent=2) + '\n')
            (staged / 'release.json').write_text(json.dumps({'release': release_id, 'public_redactions': 1, 'write_protocol': 'public-board-5', 'reference_protocol': 'budget-v1'}) + '\n')
            for name in ('context.json', 'release.json'):
                (staged / name).chmod(0o644)
            staged.chmod(0o755)
            os.replace(staged, releases / release_id)
            if not activate:
                return release_id
            # Shared across releases so rollback cannot restore an older public view.
            replacement = published / ('.redactions-' + uuid.uuid4().hex)
            replacement.write_text(json.dumps(redactions, ensure_ascii=False) + '\n')
            replacement.chmod(0o644)
            os.replace(replacement, published / 'message-redactions.json')
            switch(published, release_id)
        finally:
            if staged.exists():
                shutil.rmtree(staged)
        return release_id


def wait_for_release(release_id, base_url, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            def fetch(route):
                response = subprocess.run(
                    ['curl', '--fail', '--silent', '--show-error', '--max-time', '3', base_url + route],
                    check=True, capture_output=True, text=True,
                )
                return json.loads(response.stdout)
            health = fetch('/healthz')
            context = fetch('/context.json')
            status = fetch('/api/status')
            if (health.get('release') == release_id and
                    context.get('deployment', {}).get('release') == release_id and
                    status.get('schema_version') == 5 and
                    status.get('storage_scope') == 'message_log_only'):
                return
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
        time.sleep(1)
    raise RuntimeError(f'{base_url} has not confirmed release {release_id}; publication is pending or failed. Previous releases remain available.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage-only', action='store_true', help='For initial host migration; skip HTTP checks')
    parser.add_argument('--rollback', metavar='RELEASE', help='Publish a retained release instead of current source')
    parser.add_argument('--prepare-v2', action='store_true', help='Stage a v2 snapshot without switching the production release')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run as maestro without sudo')
    published = SOURCE / '.published'
    if args.prepare_v2:
        parser.error('Authenticated v2 was cancelled; use normal publication')
    if not args.stage_only and not (published / '.configured').exists():
        parser.error('One-time setup required: sudo ./scripts/enable_owner_publish.sh')
    release_id = publish(SOURCE, published, args.rollback)
    print(f'Published release {release_id}', flush=True)
    if not args.stage_only:
        wait_for_release(release_id, 'http://127.0.0.1:8787')
        wait_for_release(release_id, 'https://agent.vincentmossman.com', timeout=20)
        print('Running service and public context confirmed this release.')


if __name__ == '__main__':
    main()
