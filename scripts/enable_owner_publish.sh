#!/usr/bin/env bash
set -Eeuo pipefail
# One-time migration of the existing service; subsequent publication needs no sudo.
PROJECT_DIR="/home/maestro/agent.vincentmossman.com"
PUBLISHED="${PROJECT_DIR}/.published"
if [[ "$EUID" -ne 0 ]]; then
  echo "One-time setup requires sudo; routine updates do not." >&2
  exit 1
fi
getent passwd agent-context >/dev/null
getent passwd maestro >/dev/null
[[ -f /var/lib/agent-context/context.json ]]
[[ -f "${PROJECT_DIR}/scripts/publish.py" ]]
BACKUP_DIR="/var/backups/agent-context/owner-publish-$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 0700 "${BACKUP_DIR}"
cp -a /var/lib/agent-context/context.json "${BACKUP_DIR}/context.json"
if [[ -d /opt/agent-context/app ]]; then cp -a /opt/agent-context/app "${BACKUP_DIR}/app"; fi
if [[ -d /etc/agent-context ]]; then cp -a /etc/agent-context "${BACKUP_DIR}/environment"; fi
for unit in agent-context.service agent-context-metrics.service agent-context-metrics.timer agent-context-publish.path agent-context-publish.service; do
  if [[ -e "/etc/systemd/system/${unit}" ]]; then cp -a "/etc/systemd/system/${unit}" "${BACKUP_DIR}/"; fi
  if [[ -d "/etc/systemd/system/${unit}.d" ]]; then cp -a "/etc/systemd/system/${unit}.d" "${BACKUP_DIR}/"; fi
done
echo "Backup: ${BACKUP_DIR}"
trap 'echo "Setup failed. Backup: ${BACKUP_DIR}" >&2; systemctl status agent-context.service --no-pager --full || true; journalctl -u agent-context.service -n 25 --no-pager || true' ERR

# Preserve previously curated extra fields, including any recent_work entries.
python3 - "${PROJECT_DIR}/context.json" <<'MERGE'
import json, os, tempfile
from pathlib import Path
import sys
source=Path(sys.argv[1])
old=json.loads(Path('/var/lib/agent-context/context.json').read_text())
new=json.loads(source.read_text())
merged={**old, **new}
if merged != new:
    stat=source.stat()
    fd,tmp=tempfile.mkstemp(dir=source.parent, prefix='.context-migration-')
    with os.fdopen(fd,'w') as stream:
        json.dump(merged,stream,indent=2,ensure_ascii=False)
        stream.write('\n')
    os.chmod(tmp,stat.st_mode & 0o777)
    os.chown(tmp,stat.st_uid,stat.st_gid)
    os.replace(tmp,source)
MERGE
runuser -u maestro -- python3 "${PROJECT_DIR}/scripts/publish.py" --stage-only
install -d -o root -g root -m 0755 /opt/agent-context/releases /etc/systemd/system/agent-context.service.d /etc/systemd/system/agent-context-metrics.service.d
for unit in agent-context.service agent-context-metrics.service agent-context-metrics.timer; do
  install -o root -g root -m 0644 "${PROJECT_DIR}/deploy/${unit}" "/etc/systemd/system/${unit}"
done
cat > /etc/systemd/system/agent-context.service.d/50-owner-publish.conf <<EOF
[Service]
WorkingDirectory=/opt/agent-context/releases/current
ExecStart=
ExecStart=/usr/bin/python3 /opt/agent-context/releases/current/server.py
Environment=AGENT_CONTEXT_FILE=/opt/agent-context/releases/current/context.json
ProtectHome=tmpfs
BindReadOnlyPaths=${PUBLISHED}:/opt/agent-context/releases
EOF
cat > /etc/systemd/system/agent-context-metrics.service.d/50-owner-publish.conf <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /opt/agent-context/releases/current/scripts/update_live_metrics.py
ProtectHome=tmpfs
BindReadOnlyPaths=${PUBLISHED}:/opt/agent-context/releases
EOF
cat > /etc/systemd/system/agent-context-publish.path <<EOF
[Unit]
Description=Watch owner publication requests for Agent Context
[Path]
PathChanged=${PUBLISHED}/restart
Unit=agent-context-publish.service
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/agent-context-publish.service <<'UNIT'
[Unit]
Description=Restart only Agent Context after owner publication
[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart agent-context.service
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
RestrictAddressFamilies=AF_UNIX
UNIT
install -d -o root -g root -m 0755 /etc/agent-context
python3 - /etc/agent-context/agent-context.env <<'PY'
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
salt_lines = [line for line in lines if line.startswith("AGENT_RATE_SALT=")]
if len(salt_lines) == 1 and re.fullmatch(r"AGENT_RATE_SALT=[a-f0-9]{64}", salt_lines[0]):
    print("Private rate-limit salt is present.")
    raise SystemExit(0)

preserved = [line for line in lines if not line.startswith("AGENT_RATE_SALT=")]
preserved.append(f"AGENT_RATE_SALT={secrets.token_hex(32)}")
fd, temporary = tempfile.mkstemp(prefix=".agent-context.env.", dir=path.parent, text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write("\n".join(preserved) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary, 0o600)
    os.chown(temporary, 0, 0)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print("Generated a private rate-limit salt; other environment settings were preserved.")
PY

systemctl daemon-reload
systemctl enable agent-context.service
systemctl restart agent-context.service
runuser -u maestro -- python3 - "${PROJECT_DIR}" <<'READY'
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'scripts'))
from publish import wait_for_release
release=json.loads((Path(sys.argv[1])/'.published/current/release.json').read_text())['release']
wait_for_release(release,'http://127.0.0.1:8787')
READY
systemctl enable --now agent-context-publish.path agent-context-metrics.timer
systemctl start agent-context-metrics.service
runuser -u maestro -- touch "${PUBLISHED}/.configured"
install -o root -g root -m 0755 "${PROJECT_DIR}/scripts/post_task_summary.sh" /usr/local/bin/agent-context-post
trap - ERR
echo "Owner publication enabled. Future updates: ${PROJECT_DIR}/scripts/update_live.sh (without sudo)."
