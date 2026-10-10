"""Persistence of the shared HALT flag; the first cause wins."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path

from ..host import Host


def write_halt(host: Host, *, reason: str, source: str, now: datetime | None = None) -> Path:
    path = host.halt_flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    now = now or datetime.now(timezone.utc)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"reason": reason, "source": source,
                   "set_at": now.astimezone(timezone.utc).isoformat(timespec="seconds")}, f)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def read_halt(host: Host) -> dict | None:
    path = host.halt_flag_path()
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"reason": text.strip(), "source": "unknown", "set_at": None}
