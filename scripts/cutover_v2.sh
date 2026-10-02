#!/usr/bin/env bash
set -Eeuo pipefail
echo "Authenticated v2 was cancelled by the owner. Use the public board workflow in README.md." >&2
exit 1
PROJECT=/home/maestro/agent.vincentmossman.com
PUB="$PROJECT/.published"
MODE="${1:-}"
[[ $EUID == 0 ]] || { echo 'Run with sudo after reviewing V2_DEPLOYMENT.md.' >&2; exit 1; }
pause_remote() {
  printf '%s\n' 'return 503;' > /etc/nginx/snippets/agent-mcp-proxy-v2.conf
  nginx -t && systemctl reload nginx
}
case "$MODE" in
  pause-remote)
    [[ -e "$PUB/.v2-cutover" ]]
    pause_remote
    echo 'Remote MCP paused. Public writes stay denied; public reads and local appends remain available.'
    exit 0 ;;
  resume-remote)
    [[ -e "$PUB/.v2-cutover" ]]
    install -m 0644 "$PROJECT/deploy/v2/agent-mcp-proxy.conf" /etc/nginx/snippets/agent-mcp-proxy-v2.conf
    nginx -t
    systemctl reload nginx
    echo 'Authenticated MCP ingress restored.'
    exit 0 ;;
  activate)
    [[ "${2:-}" == --replacement-tested ]] || { echo 'Link and test the replacement ChatGPT connection first, then pass --replacement-tested.' >&2; exit 1; } ;;
  *) echo 'Usage: cutover_v2.sh activate --replacement-tested | pause-remote | resume-remote' >&2; exit 2 ;;
esac
[[ -e "$PUB/.v2-installed" ]]
[[ ! -e "$PUB/.v2-complete" ]] || { echo 'Already activated.'; exit 0; }
systemctl is-active --quiet agent-context-write.service agent-context-mcp-v2.service
/opt/agent-context/mcp-venv/bin/python "$PROJECT/scripts/check_oauth_provider.py" /etc/agent-context/oauth-v2.json
python3 - <<'PY'
import json
from pathlib import Path
if not json.loads(Path('/etc/agent-context/oauth-v2.json').read_text()).get('allowed_client'):
    raise SystemExit('The ChatGPT client must be pinned before cutover')
PY
BACKUP="/var/backups/agent-context/v2-cutover-$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 0700 "$BACKUP"
cp -a /etc/nginx/snippets/agent-mcp-location.conf "$BACKUP/legacy-mcp-location.conf"
cp -a /etc/systemd/system/agent-context-publish.service.d/60-mcp.conf "$BACKUP/60-mcp.conf"
readlink "$PUB/current" > "$BACKUP/old-release"
readlink "$PUB/v2-current" > "$BACKUP/v2-release"
# Both defenses persist independently of the selected application snapshot.
touch /etc/agent-context/public-writes-disabled
install -m 0644 "$PROJECT/deploy/v2/agent-mcp-proxy.conf" /etc/nginx/snippets/agent-mcp-proxy-v2.conf
install -m 0644 "$PROJECT/deploy/v2/agent-mcp-cutover.conf" /etc/nginx/snippets/agent-mcp-location.conf
nginx -t
systemctl reload nginx
# From this point failures never restore anonymous posting.
trap 'pause_remote; echo "Cutover incomplete; remote MCP paused, anonymous writes remain closed. Backup: $BACKUP" >&2' ERR
python3 - "$PUB" <<'PY'
import json, os, sys
from pathlib import Path
p=Path(sys.argv[1]); release=(p/'v2-current').resolve()
manifest=json.loads((release/'release.json').read_text())
if manifest.get('write_protocol') != 2 or release.parent != (p/'releases').resolve():
    raise SystemExit('Invalid v2 snapshot')
tmp=p/'.current-v2-cutover'; tmp.symlink_to('releases/'+release.name)
os.replace(tmp,p/'current')
tmp=p/'.v2-current-cutover'; tmp.symlink_to('current')
os.replace(tmp,p/'v2-current')
(p/'.v2-cutover').touch()
PY
cat > /etc/systemd/system/agent-context-publish.service.d/60-mcp.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/systemctl restart agent-context.service agent-context-write.service agent-context-mcp-v2.service
EOF
systemctl disable --now agent-context-mcp.service agent-context-v2-publish.path
systemctl daemon-reload
systemctl restart agent-context.service agent-context-write.service agent-context-mcp-v2.service
# Readiness checks do not create public test messages.
ready=false
for attempt in {1..30}; do
  if curl --fail --silent --unix-socket /run/agent-context-mcp-v2/mcp.sock \
      -H 'Host: agent.vincentmossman.com' http://localhost/.well-known/oauth-protected-resource/mcp-v2 >/dev/null \
      && curl --fail --silent http://127.0.0.1:8787/healthz >/dev/null; then
    ready=true; break
  fi
  sleep 1
done
[[ "$ready" == true ]]
# Empty POSTs cannot append and must be rejected before body parsing.
code=$(curl --silent -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8787/api/messages)
[[ "$code" == 403 ]]
touch "$PUB/.v2-complete"
trap - ERR
echo 'Cutover complete. /mcp-v2 and /mcp use the authenticated bridge; anonymous REST writes are denied.'
echo "Backup: $BACKUP"
echo 'Routine updates: ./scripts/update_live.sh as maestro. Emergency rollback: sudo ./scripts/cutover_v2.sh pause-remote'
