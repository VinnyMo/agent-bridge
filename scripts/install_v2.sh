#!/usr/bin/env bash
set -Eeuo pipefail
echo "Authenticated v2 was cancelled by the owner. Use the public board workflow in README.md." >&2
exit 1
PROJECT=/home/maestro/agent.vincentmossman.com
SITE=/etc/nginx/sites-available/agent.vincentmossman.com
[[ $EUID == 0 ]] || { echo 'Run with sudo after completing V2_DEPLOYMENT.md.' >&2; exit 1; }
[[ -f "$PROJECT/.published/v2-current/bridge_v2.py" ]] || { echo 'First run ./scripts/publish.py --prepare-v2 as maestro.' >&2; exit 1; }
[[ ! -e "$PROJECT/.published/.v2-cutover" ]] || { echo 'Already cut over; use routine publication.' >&2; exit 1; }
[[ -f /etc/agent-context/oauth-v2.json ]] || { echo 'Configure /etc/agent-context/oauth-v2.json using deploy/v2/oauth-v2.example.json first.' >&2; exit 1; }
/opt/agent-context/mcp-venv/bin/python "$PROJECT/scripts/check_oauth_provider.py" /etc/agent-context/oauth-v2.json
BACKUP="/var/backups/agent-context/v2-stage-$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 0700 "$BACKUP"
cp -a "$SITE" "$BACKUP/site"
# Download public CA material only; TLS verification stays on.
for ca in openai-root-ca openai-connectors-mtls-ca; do
  curl --fail --silent --show-error --proto '=https' --max-time 30 \
    "https://developers.openai.com/plugins/mtls/${ca}.pem" -o "$BACKUP/${ca}.pem"
  openssl x509 -in "$BACKUP/${ca}.pem" -noout -checkend 86400 >/dev/null
  install -m 0644 "$BACKUP/${ca}.pem" "/etc/agent-context/${ca}.pem"
done
openssl verify -CAfile /etc/agent-context/openai-root-ca.pem /etc/agent-context/openai-connectors-mtls-ca.pem
cat /etc/agent-context/openai-connectors-mtls-ca.pem /etc/agent-context/openai-root-ca.pem > /etc/agent-context/openai-mtls-chain.pem
chmod 0644 /etc/agent-context/openai-mtls-chain.pem
getent group agent-context-writers >/dev/null || groupadd --system agent-context-writers
id agent-context-mcp-v2 >/dev/null 2>&1 || useradd --system --user-group --no-create-home --shell /usr/sbin/nologin agent-context-mcp-v2
usermod -aG agent-context-writers maestro
chown root:agent-context-mcp-v2 /etc/agent-context/oauth-v2.json
chmod 0640 /etc/agent-context/oauth-v2.json
for unit in agent-context-write.service agent-context-mcp-v2.service; do
  install -m 0644 "$PROJECT/deploy/v2/$unit" "/etc/systemd/system/$unit"
done
install -m 0644 "$PROJECT/deploy/v2/agent-mcp-proxy.conf" /etc/nginx/snippets/agent-mcp-proxy-v2.conf
install -m 0644 "$PROJECT/deploy/v2/agent-mcp-v2-routes.conf" /etc/nginx/snippets/agent-mcp-v2-routes.conf
python3 - "$SITE" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); text=p.read_text()
line='    include /etc/nginx/snippets/agent-mcp-v2-routes.conf;'
anchor='    include /etc/nginx/snippets/agent-mcp-location.conf;'
if line not in text:
    if text.count(anchor) != 1:
        raise SystemExit('Unexpected nginx layout; refusing to edit')
    p.write_text(text.replace(anchor,anchor+'\n'+line))
PY
restore_site() { cp -a "$BACKUP/site" "$SITE"; nginx -t && systemctl reload nginx; }
trap 'restore_site; echo "v2 staging failed; previous site restored. Backup: $BACKUP" >&2' ERR
nginx -t
systemctl daemon-reload
systemctl enable --now agent-context-write.service agent-context-mcp-v2.service
systemctl restart agent-context-write.service agent-context-mcp-v2.service
# Readiness is discovery-only; it does not append a test note.
ready=false
for attempt in {1..30}; do
  if curl --fail --silent --unix-socket /run/agent-context-mcp-v2/mcp.sock \
    -H 'Host: agent.vincentmossman.com' http://localhost/.well-known/oauth-protected-resource/mcp-v2 > "$BACKUP/discovery.json"; then
    ready=true; break
  fi
  sleep 1
done
[[ "$ready" == true ]]
cat > /etc/systemd/system/agent-context-v2-publish.path <<'EOF'
[Unit]
Description=Watch staged v2 source updates
[Path]
PathChanged=/home/maestro/agent.vincentmossman.com/.published/v2-restart
Unit=agent-context-v2-publish.service
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/agent-context-v2-publish.service <<'EOF'
[Unit]
Description=Restart only the staged v2 bridge and local writer
[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart agent-context-write.service agent-context-mcp-v2.service
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
RestrictAddressFamilies=AF_UNIX
EOF
systemctl daemon-reload
systemctl enable --now agent-context-v2-publish.path
systemctl reload nginx
install -m 0755 "$PROJECT/scripts/post_local_note.sh" /usr/local/bin/agent-context-post
trap - ERR
touch "$PROJECT/.published/.v2-installed"
echo 'v2 staged at /mcp-v2. Legacy /mcp and public REST writes are unchanged.'
echo 'Cloudflare-proxied TLS cannot meet this ingress check. Complete the direct-TLS and Auth0 linking steps before cutover.'
echo "Backup: $BACKUP"
