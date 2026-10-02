#!/usr/bin/env bash
set -Eeuo pipefail

# Install the HTTPS reverse-proxy site after DNS, certificate, and app are ready.
# Usage: sudo bash scripts/install_nginx.sh

DOMAIN="agent.vincentmossman.com"
SITE_FILE="/etc/nginx/sites-available/${DOMAIN}"
ENABLED_FILE="/etc/nginx/sites-enabled/${DOMAIN}"
LIMIT_FILE="/etc/nginx/conf.d/agent-context-limit.conf"
CERT="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
KEY="/etc/letsencrypt/live/${DOMAIN}/privkey.pem"
UPSTREAM="127.0.0.1:8787"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo or as root." >&2
  exit 1
fi

if ! command -v nginx >/dev/null 2>&1 || [[ ! -d /etc/nginx/conf.d ]]; then
  echo "nginx is not installed." >&2
  exit 1
fi

if [[ ! -s "${CERT}" || ! -s "${KEY}" ]]; then
  echo "TLS certificate not found at ${CERT} and ${KEY}. Provision it after DNS resolves, then retry." >&2
  exit 1
fi

if ! timeout 2 bash -c '</dev/tcp/127.0.0.1/8787' 2>/dev/null; then
  echo "No app is accepting connections on ${UPSTREAM}; refusing to install a broken site." >&2
  exit 1
fi

tmp_file="$(mktemp /etc/nginx/sites-available/.${DOMAIN}.XXXXXX)"
tmp_limit="$(mktemp /etc/nginx/conf.d/.agent-context-limit.XXXXXX)"
backup_file=""
backup_limit=""
cleanup() { rm -f "${tmp_file}" "${tmp_limit}"; }
trap cleanup EXIT

cat >"${tmp_limit}" <<'NGINX'
# Short-term burst protection for unauthenticated public posts.
limit_req_zone $binary_remote_addr zone=agent_context_post:10m rate=30r/m;
NGINX

cat >"${tmp_file}" <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name agent.vincentmossman.com;
    access_log off;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name agent.vincentmossman.com;

    ssl_certificate     /etc/letsencrypt/live/agent.vincentmossman.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/agent.vincentmossman.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    # Trust CF-Connecting-IP only when the peer is in Cloudflare's published
    # proxy ranges; direct origin requests continue to use their socket IP.
    set_real_ip_from 173.245.48.0/20;
    set_real_ip_from 103.21.244.0/22;
    set_real_ip_from 103.22.200.0/22;
    set_real_ip_from 103.31.4.0/22;
    set_real_ip_from 141.101.64.0/18;
    set_real_ip_from 108.162.192.0/18;
    set_real_ip_from 190.93.240.0/20;
    set_real_ip_from 188.114.96.0/20;
    set_real_ip_from 197.234.240.0/22;
    set_real_ip_from 198.41.128.0/17;
    set_real_ip_from 162.158.0.0/15;
    set_real_ip_from 104.16.0.0/13;
    set_real_ip_from 104.24.0.0/14;
    set_real_ip_from 172.64.0.0/13;
    set_real_ip_from 131.0.72.0/22;
    set_real_ip_from 2400:cb00::/32;
    set_real_ip_from 2606:4700::/32;
    set_real_ip_from 2803:f800::/32;
    set_real_ip_from 2405:b500::/32;
    set_real_ip_from 2405:8100::/32;
    set_real_ip_from 2a06:98c0::/29;
    set_real_ip_from 2c0f:f248::/32;
    real_ip_header CF-Connecting-IP;
    real_ip_recursive on;

    server_tokens off;
    access_log off;
    client_max_body_size 8k;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    add_header X-Robots-Tag "noindex, nofollow, noarchive" always;
    add_header Content-Security-Policy "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; connect-src 'self'" always;

    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_connect_timeout 3s;
    proxy_read_timeout 15s;

    location = /api/messages {
        limit_req zone=agent_context_post burst=10 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:8787;
    }

    location / {
        proxy_pass http://127.0.0.1:8787;
    }
}
NGINX

chmod 0644 "${tmp_file}"
chmod 0644 "${tmp_limit}"
if [[ -e "${SITE_FILE}" ]]; then
  backup_file="${SITE_FILE}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -a "${SITE_FILE}" "${backup_file}"
fi
if [[ -e "${LIMIT_FILE}" ]]; then
  backup_limit="${LIMIT_FILE}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -a "${LIMIT_FILE}" "${backup_limit}"
fi

install -o root -g root -m 0644 "${tmp_file}" "${SITE_FILE}"
install -o root -g root -m 0644 "${tmp_limit}" "${LIMIT_FILE}"
ln -sfn "${SITE_FILE}" "${ENABLED_FILE}"

if ! nginx -t; then
  if [[ -n "${backup_file}" ]]; then
    cp -a "${backup_file}" "${SITE_FILE}"
  else
    rm -f "${SITE_FILE}" "${ENABLED_FILE}"
  fi
  if [[ -n "${backup_limit}" ]]; then cp -a "${backup_limit}" "${LIMIT_FILE}"; else rm -f "${LIMIT_FILE}"; fi
  echo "nginx validation failed; restored the previous site configuration." >&2
  exit 1
fi

if ! systemctl reload nginx; then
  if [[ -n "${backup_file}" ]]; then
    cp -a "${backup_file}" "${SITE_FILE}"
  else
    rm -f "${SITE_FILE}" "${ENABLED_FILE}"
  fi
  if [[ -n "${backup_limit}" ]]; then cp -a "${backup_limit}" "${LIMIT_FILE}"; else rm -f "${LIMIT_FILE}"; fi
  nginx -t && systemctl reload nginx || true
  echo "nginx reload failed; restored the previous site configuration." >&2
  exit 1
fi

echo "Installed ${DOMAIN} reverse proxy."
[[ -z "${backup_file}" ]] || echo "Previous config backup: ${backup_file}"
[[ -z "${backup_limit}" ]] || echo "Previous rate-limit backup: ${backup_limit}"
