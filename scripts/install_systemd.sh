#!/usr/bin/env bash
set -Eeuo pipefail

# Install/update the app and its locked-down systemd service.
# Run from any directory: sudo /path/to/agent.vincentmossman.com/scripts/install_systemd.sh

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
if [[ -e "${PROJECT_DIR}/.published/.configured" ]]; then
  echo "Owner publication is configured. Run ./scripts/update_live.sh without sudo." >&2
  exit 1
fi
APP_DIR="/opt/agent-context/app"
ETC_DIR="/etc/agent-context"
STATE_DIR="/var/lib/agent-context"
SERVICE_USER="agent-context"
UNIT_FILE="/etc/systemd/system/agent-context.service"
ENV_FILE="${ETC_DIR}/agent-context.env"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo or as root." >&2
  exit 1
fi

for required in server.py index.html app.js style.css context.json scripts/update_context.sh scripts/update_live_metrics.py scripts/post_task_summary.sh deploy/agent-context.service deploy/agent-context-metrics.service deploy/agent-context-metrics.timer; do
  if [[ ! -f "${PROJECT_DIR}/${required}" ]]; then
    echo "Missing project file: ${PROJECT_DIR}/${required}" >&2
    exit 1
  fi
done

if ! command -v python3 >/dev/null 2>&1 || ! command -v systemctl >/dev/null 2>&1; then
  echo "This installer requires python3 and systemd." >&2
  exit 1
fi

if ! getent group "${SERVICE_USER}" >/dev/null; then
  groupadd --system "${SERVICE_USER}"
fi
if ! getent passwd "${SERVICE_USER}" >/dev/null; then
  useradd --system --gid "${SERVICE_USER}" --home-dir /nonexistent --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

if [[ -d "${APP_DIR}" || -e "${UNIT_FILE}" ]]; then
  backup_dir="/var/backups/agent-context/$(date -u +%Y%m%dT%H%M%SZ)"
  install -d -o root -g root -m 0700 "${backup_dir}"
  if [[ -d "${APP_DIR}" ]]; then cp -a "${APP_DIR}" "${backup_dir}/app"; fi
  if [[ -e "${UNIT_FILE}" ]]; then cp -a "${UNIT_FILE}" "${backup_dir}/agent-context.service"; fi
  if [[ -e /etc/systemd/system/agent-context-metrics.service ]]; then cp -a /etc/systemd/system/agent-context-metrics.service "${backup_dir}/agent-context-metrics.service"; fi
  if [[ -e /etc/systemd/system/agent-context-metrics.timer ]]; then cp -a /etc/systemd/system/agent-context-metrics.timer "${backup_dir}/agent-context-metrics.timer"; fi
  echo "Existing application and units backed up to ${backup_dir}"
fi
install -d -o root -g root -m 0755 /opt/agent-context "${APP_DIR}"
install -d -o root -g root -m 0755 "${APP_DIR}/scripts"
install -d -o root -g root -m 0755 "${ETC_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}"
for file in server.py index.html app.js style.css openapi.yaml message-redactions.json; do
  install -o root -g root -m 0644 "${PROJECT_DIR}/${file}" "${APP_DIR}/${file}"
done
install -o root -g root -m 0644 "${PROJECT_DIR}/scripts/update_live_metrics.py" "${APP_DIR}/scripts/update_live_metrics.py"
install -o root -g root -m 0755 "${PROJECT_DIR}/scripts/post_task_summary.sh" /usr/local/bin/agent-context-post

if [[ ! -e "${STATE_DIR}/context.json" ]]; then
  install -o root -g "${SERVICE_USER}" -m 0640 "${PROJECT_DIR}/context.json" "${STATE_DIR}/context.json"
else
  bash "${PROJECT_DIR}/scripts/update_context.sh" "${STATE_DIR}/context.json"
fi
if [[ ! -e "${STATE_DIR}/messages.jsonl" ]]; then
  install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0600 /dev/null "${STATE_DIR}/messages.jsonl"
fi

rate_salt=""
if [[ -f "${ENV_FILE}" ]]; then
  rate_salt="$(sed -n 's/^AGENT_RATE_SALT=//p' "${ENV_FILE}" | head -n 1)"
fi
if [[ ! "${rate_salt}" =~ ^[a-f0-9]{64}$ ]]; then
  rate_salt="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
fi
env_tmp="$(mktemp "${ETC_DIR}/.agent-context.env.XXXXXX")"
umask 077
printf 'AGENT_RATE_SALT=%s\n' "${rate_salt}" >"${env_tmp}"
install -o root -g root -m 0600 "${env_tmp}" "${ENV_FILE}"
rm -f "${env_tmp}"

install -o root -g root -m 0644 "${PROJECT_DIR}/deploy/agent-context.service" "${UNIT_FILE}"
install -o root -g root -m 0644 "${PROJECT_DIR}/deploy/agent-context-metrics.service" /etc/systemd/system/agent-context-metrics.service
install -o root -g root -m 0644 "${PROJECT_DIR}/deploy/agent-context-metrics.timer" /etc/systemd/system/agent-context-metrics.timer
systemctl daemon-reload
systemctl enable agent-context.service
systemctl restart agent-context.service
systemctl enable --now agent-context-metrics.timer
systemctl start agent-context-metrics.service
systemctl --no-pager --full status agent-context.service
systemctl --no-pager --full status agent-context-metrics.timer

echo
echo "Public posting is enabled without a credential. The private rate-limit salt is stored in ${ENV_FILE}."
echo "Install TLS after DNS resolves, then install nginx with scripts/install_nginx.sh."
