"""Append-only JSONL audit.

Invariant 8: every preview and charge is logged with timestamp, session, cart and outcome.
Profile approvals are logged here too — who signed off on the domain claims the agent is
about to make out loud is exactly the kind of thing you want a record of.

Append-only and best-effort: a failed write must never fail a charge that succeeded.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

_lock = threading.Lock()


def record(event: str, **payload: Any) -> dict:
    entry = {
        "event": event,
        "at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }

    settings = get_settings()
    line = json.dumps(entry, default=str, ensure_ascii=False)

    try:
        with _lock:
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            with settings.audit_log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError as exc:
        log.error("audit write failed for %s: %s", event, exc)

    log.info("audit %s | %s", event, line)
    return entry


def read_all() -> list[dict]:
    """For the trace endpoint and for tests. Small volumes by construction."""
    path = get_settings().audit_log_path
    if not path.exists():
        return []
    entries: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries
