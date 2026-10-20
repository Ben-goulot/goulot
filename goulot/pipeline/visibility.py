"""Public and private events (docs/SPEC.md, sections 5 and 11).

An event is public when it is linked to a node of the typical chain or when one of its articles triggered a stored
public rule: the view `public_events` (migration 0012). An event found only through Ma chaîne (an article from an
in-memory query, a hazard near a private site) is private: it never enters the market verdict, the weekly digest,
the « nouvel événement » alert or any response outside /api/private/.
"""

from __future__ import annotations

import sqlite3


def is_public(conn: sqlite3.Connection, event_id: str) -> bool:
    return conn.execute("SELECT 1 FROM public_events WHERE id = ?", (event_id,)).fetchone() is not None


def serious_event(conn: sqlite3.Connection) -> bool:
    """A confirmed public event of severity hint ≥ 3 under way (offer to raise the watch cap for the month)."""
    return conn.execute("SELECT 1 FROM public_events WHERE confirmation = 'confirmed' AND severity_hint >= 3 "
                        "AND event_status IN ('ongoing', 'escalating') LIMIT 1").fetchone() is not None
