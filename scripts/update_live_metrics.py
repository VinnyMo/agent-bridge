#!/usr/bin/env python3
"""Write a coarse, public-safe host summary and fixed-URL reachability checks."""

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OUTPUT = Path(os.environ.get("AGENT_LIVE_METRICS_FILE", "/var/lib/agent-context/live-metrics.json"))
PROJECTS = (
    ("agent.vincentmossman.com", "https://agent.vincentmossman.com/healthz"),
    ("ICBINY", "https://icbiny.com/"),
    ("vincentmossman.com", "https://vincentmossman.com/"),
    ("THE ENTER GAME", "https://vincentmossman.com/ti-emulator/"),
    ("Fun With Primes", "https://vincentmossman.com/prime-generator/"),
    ("Ubuntu Maintenance PPA", "https://launchpad.net/~vinny-mossman/+archive/ubuntu/ubuntumaintenance"),
    ("InPlainSight", "https://vincentmossman.com/inplainsight/"),
)


def rounded_percent(value: float) -> int:
    return max(0, min(100, int(round(value / 5) * 5)))


def host_metrics() -> dict:
    load = os.getloadavg()[0]
    cpu_count = max(1, os.cpu_count() or 1)
    memory_total = memory_available = 0
    with open("/proc/meminfo", encoding="ascii") as source:
        for line in source:
            key, _, rest = line.partition(":")
            value = rest.strip().split()
            if value:
                if key == "MemTotal":
                    memory_total = int(value[0])
                elif key == "MemAvailable":
                    memory_available = int(value[0])
    memory_percent = 100 * (memory_total - memory_available) / memory_total if memory_total else 0
    disk = shutil.disk_usage("/")
    uptime_seconds = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
    return {
        "cpu_load_1m_percent_of_capacity": rounded_percent(100 * load / cpu_count),
        "memory_used_percent": rounded_percent(memory_percent),
        "root_filesystem_used_percent": rounded_percent(100 * disk.used / disk.total),
        "uptime_days_rounded": max(0, round(uptime_seconds / 86400)),
    }


def check_project(name: str, url: str, timestamp: str) -> dict:
    request = Request(url, headers={"User-Agent": "AgentContext-HealthCheck/1.0"})
    try:
        with urlopen(request, timeout=5) as response:
            reachable = 200 <= response.status < 400
    except HTTPError as error:
        reachable = 200 <= error.code < 400
    except (OSError, URLError, TimeoutError, ValueError):
        reachable = False
    return {"name": name, "status": "reachable" if reachable else "unreachable", "checked_at": timestamp}


def atomic_write(value: dict) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    original_mode = 0o640
    if OUTPUT.exists():
        original_mode = OUTPUT.stat().st_mode & 0o777
    fd, temporary = tempfile.mkstemp(prefix=".live-metrics.", dir=OUTPUT.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, original_mode)
        os.replace(temporary, OUTPUT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = {
        "updated_at": timestamp,
        "resource_summary": host_metrics(),
        "project_reachability": [check_project(name, url, timestamp) for name, url in PROJECTS],
        "note": "Coarse host figures; project states mean public URL reachable, not full application health.",
    }
    atomic_write(result)


if __name__ == "__main__":
    main()
