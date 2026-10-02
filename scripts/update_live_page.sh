#!/usr/bin/env bash
# Compatibility entry point: publish the complete, consistent version.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/update_live.sh" "$@"
