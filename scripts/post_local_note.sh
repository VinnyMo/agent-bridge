#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export AGENT_POST_URL=http://127.0.0.1:8787/api/messages
exec "$SCRIPT_DIR/post_task_summary.sh" "$@"
