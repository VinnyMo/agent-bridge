#!/usr/bin/env python3
"""Show local, pseudonymous activity for the agent message endpoint."""

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE = Path("/var/lib/agent-context")
MESSAGE_LOG = STATE / "messages.jsonl"
ABUSE_DB = STATE / "abuse.sqlite3"
start_day = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()
activity: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"accepted": 0, "blocked": 0})

if MESSAGE_LOG.exists():
    with MESSAGE_LOG.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            day, ip_hash = item.get("utc_day"), item.get("ip_hash")
            if isinstance(day, str) and day >= start_day and isinstance(ip_hash, str):
                activity[(day, ip_hash)]["accepted"] += 1

if ABUSE_DB.exists():
    with sqlite3.connect(f"file:{ABUSE_DB}?mode=ro", uri=True) as db:
        try:
            rows = db.execute(
                "SELECT utc_day, ip_hash, hits FROM rate_limited WHERE utc_day >= ?",
                (start_day,),
            )
            for day, ip_hash, hits in rows:
                activity[(day, ip_hash)]["blocked"] = int(hits)
        except sqlite3.OperationalError:
            pass

accepted = sum(values["accepted"] for values in activity.values())
blocked = sum(values["blocked"] for values in activity.values())
print(f"Agent message activity, last 7 UTC days (since {start_day})")
print(f"Accepted messages: {accepted}")
print(f"Rate-limited attempts: {blocked}")
print("Source identifiers below are keyed hashes; raw client IPs are not stored.")
for (day, ip_hash), values in sorted(activity.items(), key=lambda item: (item[0][0], -item[1]["blocked"], -item[1]["accepted"])):
    print(f"{day} source={ip_hash[:16]} accepted={values['accepted']} blocked={values['blocked']}")
