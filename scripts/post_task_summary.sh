#!/usr/bin/env bash
set -Eeuo pipefail
[[ $# == 2 ]] || { echo 'Usage: post_task_summary.sh AGENT "public message"' >&2; exit 2; }
python3 - "$1" "$2" <<'PY' | curl --fail-with-body --silent --show-error --max-time 15 \
  -H 'Content-Type: application/json' --data-binary @- \
  "${AGENT_POST_URL:-https://agent.vincentmossman.com/api/messages}"
import json,sys
agent,message=sys.argv[1:]
if not agent.strip() or len(agent)>40 or not message.strip() or len(message)>7000:
    raise SystemExit('Agent must be 1–40 characters; message must be 1–7000 characters')
if any(ord(c)<32 and c not in '\n\t' for c in message) or any(ord(c)<32 for c in agent):
    raise SystemExit('Control characters are not allowed')
if len(json.dumps(message,ensure_ascii=True))-2>7000:
    raise SystemExit('Message exceeds 7,000 bytes after JSON escaping')
raw=json.dumps({'agent':agent,'message':message},ensure_ascii=False).encode()
if len(raw)>8192: raise SystemExit('Request exceeds 8 KiB')
sys.stdout.buffer.write(raw)
PY
printf '\n'
