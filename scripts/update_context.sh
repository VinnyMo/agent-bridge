#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "Usage: update_context.sh /path/to/context.json" >&2
  exit 2
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "Run with sudo or as root." >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${SCRIPT_DIR}/../context.json"
TARGET="$1"
python3 - "$SOURCE" "$TARGET" <<'PY'
import json
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

source, target = map(Path, sys.argv[1:])
with source.open(encoding="utf-8") as handle:
    canonical = json.load(handle)
with target.open(encoding="utf-8") as handle:
    existing = json.load(handle)
if not isinstance(canonical, dict) or not isinstance(existing, dict):
    raise SystemExit("Both context files must contain JSON objects")

for key in ("schema_version", "purpose", "handling", "operator_preferences", "host_summary", "projects", "my_stuff", "message_board"):
    existing[key] = canonical[key]
existing["updated_at"] = date.today().isoformat()
with target.open(encoding="utf-8") as handle:
    previous = json.load(handle)
if existing == previous:
    print("Context is already current.")
    raise SystemExit(0)

stat = target.stat()
backup = target.with_name(f"{target.name}.bak.{date.today():%Y%m%d}")
if backup.exists():
    backup = target.with_name(f"{backup.name}.{os.getpid()}")
shutil.copy2(target, backup)
fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent, text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(existing, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, stat.st_mode & 0o777)
    os.chown(temporary, stat.st_uid, stat.st_gid)
    os.replace(temporary, target)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print(f"Updated curated context at {target}; backup: {backup}")
PY
