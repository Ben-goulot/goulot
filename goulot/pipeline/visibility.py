"""Public and private events (docs/SPEC.md, sections 5 and 11).

An event is public when it is linked to a node of the typical chain or when one of its articles triggered a stored
public rule: the view `public_events` (migration 0012). An event found only through Ma chaîne (an article from an
in-memory query, a hazard near a private site) is private: it never enters the market verdict, the weekly digest,
the « nouvel événement » alert or any response outside /api/private/.
"""

from __future__ import annotations

import sqlite3


def from_old_news(conn: sqlite3.Connection, event_id: str) -> bool:
    """Every article of the event was published more than 30 days before Goulot found it: an old story that
    resurfaced (rules.too_old now stops them at the collection). It enters neither a verdict nor an analysis."""
    row = conn.execute(
        "SELECT COUNT(*) AS total, SUM(a.published_at IS NOT NULL AND "
        "julianday(a.published_at) < julianday(a.first_seen_at) - 30) AS old FROM event_articles ea "
        "JOIN articles a ON a.id = ea.article_id WHERE ea.event_id = ? AND a.kind IN ('news', 'policy')",
        (event_id,)).fetchone()
    return bool(row["total"]) and row["old"] == row["total"]


def is_public(conn: sqlite3.Connection, event_id: str) -> bool:
    return conn.execute("SELECT 1 FROM public_events WHERE id = ?", (event_id,)).fetchone() is not None


def serious_event(conn: sqlite3.Connection) -> bool:
    """A confirmed public event of severity hint ≥ 3 under way (offer to raise the watch cap for the month)."""
    return conn.execute("SELECT 1 FROM public_events WHERE confirmation = 'confirmed' AND severity_hint >= 3 "
                        "AND event_status IN ('ongoing', 'escalating') LIMIT 1").fetchone() is not None
