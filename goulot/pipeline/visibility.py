"""Public and private events (docs/SPEC.md, sections 5 and 11).

An event is public when it is linked to a node of the typical chain or when one of its articles triggered a stored
public rule: the view `public_events` (migration 0012). An event found only through Ma chaîne (an article from an
in-memory query, a hazard near a private site) is private: it never enters the market verdict, the weekly digest,
the « nouvel événement » alert or any response outside /api/private/.
"""

from __future__ import annotations

import json
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


def minor_alert(conn: sqlite3.Connection, event_id: str) -> bool:
    """Born from a green GDACS alert, which GDACS itself rates as of low impact (rules.match now stops them)."""
    rows = conn.execute("SELECT a.source, a.details FROM event_articles ea JOIN articles a ON a.id = ea.article_id "
                        "WHERE ea.event_id = ?", (event_id,)).fetchall()
    return bool(rows) and all(r["source"] == "gdacs" and str(json.loads(r["details"]).get("alert_level", "")).lower()
                              == "green" for r in rows)


def from_headlines(conn: sqlite3.Connection, event_id: str) -> bool:
    """Every news article of the event carried its headline only (decision of 4 November 2026; rules.title_only now
    stops them at the collection)."""
    from goulot.collect.rules import headline_only

    rows = conn.execute("SELECT a.title, a.excerpt FROM event_articles ea JOIN articles a ON a.id = ea.article_id "
                        "WHERE ea.event_id = ? AND a.kind = 'news'", (event_id,)).fetchall()
    others = conn.execute("SELECT COUNT(*) FROM event_articles ea JOIN articles a ON a.id = ea.article_id "
                          "WHERE ea.event_id = ? AND a.kind != 'news'", (event_id,)).fetchone()[0]
    return bool(rows) and not others and all(headline_only(r["title"], r["excerpt"]) for r in rows)


def set_aside(conn: sqlite3.Connection, event_id: str) -> bool:
    """An event that enters neither a verdict, nor an analysis, nor a screen."""
    return from_old_news(conn, event_id) or minor_alert(conn, event_id) or from_headlines(conn, event_id)


def is_public(conn: sqlite3.Connection, event_id: str) -> bool:
    return conn.execute("SELECT 1 FROM public_events WHERE id = ?", (event_id,)).fetchone() is not None


def serious_event(conn: sqlite3.Connection) -> bool:
    """A confirmed public event of severity hint ≥ 3 under way (offer to raise the watch cap for the month)."""
    return conn.execute("SELECT 1 FROM public_events WHERE confirmation = 'confirmed' AND severity_hint >= 3 "
                        "AND event_status IN ('ongoing', 'escalating') LIMIT 1").fetchone() is not None
