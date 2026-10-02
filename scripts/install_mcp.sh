#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT=/home/maestro/agent.vincentmossman.com
SITE=/etc/nginx/sites-available/agent.vincentmossman.com
[[ $EUID == 0 ]] || { echo "Run once with sudo: $0" >&2; exit 1; }
[[ -f "$PROJECT/.published/current/mcp_bridge.py" ]] || { echo 'Run ./scripts/update_live.sh first.' >&2; exit 1; }
BACKUP="/var/backups/agent-context/mcp-$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 0700 "$BACKUP"
cp -a "$SITE" "$BACKUP/site"
# Dependencies are installed once, isolated from the existing REST service.
python3 -m venv /opt/agent-context/mcp-venv
/opt/agent-context/mcp-venv/bin/pip install -r "$PROJECT/requirements-mcp.txt"
install -m 0644 "$PROJECT/deploy/agent-context-mcp.service" /etc/systemd/system/agent-context-mcp.service
install -d -m 0755 /etc/nginx/snippets /etc/systemd/system/agent-context-publish.service.d
install -m 0644 "$PROJECT/deploy/agent-mcp-location.conf" /etc/nginx/snippets/agent-mcp-location.conf
cat > /etc/nginx/conf.d/agent-mcp-limit.conf <<'EOF'
limit_req_zone $binary_remote_addr zone=agent_mcp_requests:10m rate=60r/m;
EOF
# Refresh trusted Cloudflare ranges from Cloudflare itself. Never trust an
# arbitrary caller's CF-Connecting-IP or X-Real-IP header.
python3 - <<'PY'
import ipaddress
import subprocess
from pathlib import Path
lines = ['# Cloudflare proxy sources; refreshed during MCP installation.']
for family, version in (('ips-v4', 4), ('ips-v6', 6)):
    # Cloudflare rejects urllib's default client on this host; use curl with
    # normal TLS verification and fail closed on an unsuccessful download.
    try:
        result = subprocess.run(
            ['curl', '--fail', '--silent', '--show-error', '--proto', '=https',
             '--connect-timeout', '5', '--max-time', '20', '--max-filesize', '16384',
             'https://www.cloudflare.com/' + family],
            check=True, capture_output=True, text=True, timeout=25,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f'Unable to download Cloudflare {family}; site routing was not changed. Rerun this installer.') from exc
    ranges = result.stdout.splitlines()
    if not ranges:
        raise RuntimeError('Cloudflare range list unavailable')
    for value in ranges:
        network = ipaddress.ip_network(value)
        if network.version != version or network.prefixlen == 0:
            raise RuntimeError('Unexpected Cloudflare proxy range; refusing configuration')
        lines.append(f'set_real_ip_from {network};')
lines += ['real_ip_header CF-Connecting-IP;', 'real_ip_recursive on;']
Path('/etc/nginx/snippets/agent-mcp-realip.conf').write_text('\n'.join(lines) + '\n')
p = Path('/etc/nginx/sites-available/agent.vincentmossman.com')
s = p.read_text()
marker = '    location / {'
if 'include /etc/nginx/snippets/agent-mcp-location.conf;' not in s:
    if s.count(marker) != 1:
        raise RuntimeError('Unexpected site layout; original configuration preserved')
    s = s.replace(marker, '    include /etc/nginx/snippets/agent-mcp-realip.conf;\n    include /etc/nginx/snippets/agent-mcp-location.conf;\n\n' + marker)
    p.write_text(s)
PY
if ! nginx -t; then
  cp -a "$BACKUP/site" "$SITE"
  echo "nginx validation failed; previous site restored. Backup: $BACKUP" >&2
  exit 1
fi
# The fixed restart hook never executes repository scripts as root.
cat > /etc/systemd/system/agent-context-publish.service.d/60-mcp.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/systemctl restart agent-context.service agent-context-mcp.service
EOF
systemctl daemon-reload
systemctl enable --now agent-context-mcp.service
systemctl restart agent-context-mcp.service
ready=false
for attempt in {1..20}; do
  if curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8765/mcp \
      -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
      --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"installer","version":"1"}}}' \
      > "$BACKUP/mcp-initialize.json" 2>/dev/null; then
    ready=true
    break
  fi
  sleep 1
done
if [[ $ready != true ]]; then
  cp -a "$BACKUP/site" "$SITE"
  echo "MCP did not become ready; nginx was not reloaded. Inspect agent-context-mcp.service." >&2
  exit 1
fi
if ! python3 - "$BACKUP/mcp-initialize.json" <<'PY'
import json, sys
with open(sys.argv[1]) as stream:
    response = json.load(stream)
if response.get('result', {}).get('serverInfo', {}).get('name') != 'Vinny Agent Bridge':
    raise SystemExit('MCP initialization did not confirm the expected service')
PY
then
  cp -a "$BACKUP/site" "$SITE"
  echo 'Initialization failed; nginx was not reloaded.' >&2
  exit 1
fi
if ! systemctl reload nginx; then
  cp -a "$BACKUP/site" "$SITE"
  nginx -t && systemctl reload nginx
  echo "Reload failed; previous site restored. Backup: $BACKUP" >&2
  exit 1
fi
touch "$PROJECT/.published/.mcp-configured"
echo "MCP installed at https://agent.vincentmossman.com/mcp"
echo "Backup: $BACKUP"
echo 'Future source updates: ./scripts/update_live.sh (as maestro).'
