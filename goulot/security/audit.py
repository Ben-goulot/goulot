"""Security journal (`audit_log`). Details must never contain private data or secrets."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from goulot.db.connection import utcnow

log = logging.getLogger("goulot.audit")


def audit(conn: sqlite3.Connection, kind: str, **detail: Any) -> None:
    conn.execute(
        "INSERT INTO audit_log (at, kind, detail) VALUES (?, ?, ?)",
        (utcnow(), kind, json.dumps(detail, sort_keys=True, ensure_ascii=False)),
    )
    log.info("audit %s", kind)
