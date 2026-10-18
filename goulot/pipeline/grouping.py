"""Events (docs/SPEC.md, section 4, steps 4 and 5).

- Triaged relevant articles join an event of the same material within 7 days: a type of the same family (the
  triage may call one fire an outage or an infrastructure failure), then a shared upstream node, the same place or a
  shared company, site or port, or else the same country and similar titles; otherwise they open a new one. The
  purchased material itself is matched by nearly every article: it never links two articles alone.
- Hazard and logistics items come from official measurements (GDACS, USGS, NOAA, PortWatch, PEGELONLINE): the
  code turns them into events without AI. A satellite fire detection near a site stays an early signal.
- After each change, the event's corroboration is recomputed from its articles' lineages.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from goulot.collect.base import fold
from goulot.db.connection import utcnow
from goulot.pipeline.corroborate import ArticleRef, article_reliability, corroborate

WINDOW = timedelta(days=7)
TITLE_SIMILARITY = 0.35
MEASUREMENT_RELIABILITY = {
    "gdacs": 0.9, "usgs_earthquakes": 0.9, "noaa_nhc": 0.9, "imf_portwatch_chokepoints": 0.9, "pegelonline": 0.9,
    "nasa_firms": 0.5,  # a hot spot near a site may be a flare: corroboration by the press is needed
}
TYPE_FAMILIES = (
    {"industrial_outage", "infrastructure_failure", "natural_disaster", "extreme_weather", "cyber_incident"},
    {"sanctions_trade_policy", "export_restriction", "regulatory_change"},
    {"labor_action", "civil_unrest", "armed_conflict"},
)
NAMED_KINDS = {"company", "site", "port", "chokepoint"}
HAZARD_TYPES = {"FL": "natural_disaster", "EQ": "natural_disaster", "VO": "natural_disaster",
                "DR": "extreme_weather", "TC": "extreme_weather", "WF": "industrial_outage"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", fold(text))}


def _similar(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def _event_id(conn: sqlite3.Connection, day: str) -> str:
    n = conn.execute("SELECT COUNT(*) FROM events WHERE id LIKE ?", (f"EVT-{day}-%",)).fetchone()[0]
    return f"EVT-{day}-{n + 1:04d}"


def _node_shares(conn: sqlite3.Connection, material_id: str) -> dict[str, float | None]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT node_key, global_share_pct FROM chain_nodes WHERE material_id = ? AND valid_to IS NULL",
        (material_id,))}


def _link(conn: sqlite3.Connection, event_id: str, material_id: str, node_keys: list[str]) -> None:
    shares = _node_shares(conn, material_id)
    for key in node_keys:
        if key in shares:
            conn.execute("INSERT OR IGNORE INTO event_links (event_id, material_id, node_key, supply_share_pct) "
                         "VALUES (?, ?, ?, ?)", (event_id, material_id, key, shares[key]))


def refresh_corroboration(conn: sqlite3.Connection, event_id: str, extra: dict[str, float] | None = None) -> None:
    rows = conn.execute(
        "SELECT a.id, a.url, a.domain, a.provider_domain, a.title, a.excerpt, a.source, t.result "
        "FROM event_articles ea JOIN articles a ON a.id = ea.article_id "
        "LEFT JOIN triage t ON t.article_id = a.id AND t.state = 'done' WHERE ea.event_id = ?", (event_id,)).fetchall()
    refs = []
    for r in rows:
        cited = json.loads(r["result"]).get("cited_origin", "") if r["result"] else ""
        reliability = MEASUREMENT_RELIABILITY.get(r["source"]) or article_reliability(
            r["domain"], r["provider_domain"], urlsplit(r["url"]).path, extra)
        refs.append(ArticleRef(r["id"], r["url"], r["domain"], r["provider_domain"], r["title"], r["excerpt"],
                               cited, reliability))
    if not refs:
        return
    result, keys = corroborate(refs)
    for ref in refs:
        conn.execute("UPDATE event_articles SET lineage_key = ?, reliability = ? WHERE event_id = ? AND article_id = ?",
                     (keys[ref.article_id], ref.reliability, event_id, ref.article_id))
    official = any(ref.reliability >= 0.9 for ref in refs)
    conn.execute(
        "UPDATE events SET c_fait = ?, lineages = ?, best_lineage_reliability = ?, official_source = ?, "
        "confirmation = ?, updated_at = ? WHERE id = ?",
        (result.c_fait, result.lineages, result.best, int(official),
         "confirmed" if result.confirmed else "early_signal", utcnow(), event_id),
    )


def _family(event_type: str) -> set[str]:
    return next((f for f in TYPE_FAMILIES if event_type in f), {event_type})


def _names(triage: dict[str, Any]) -> set[str]:
    return {fold(e["name"]).strip() for e in triage.get("entities", []) if e["kind"] in NAMED_KINDS} - {"", "unknown"}


def _find_event(conn: sqlite3.Connection, material_id: str, t: dict[str, Any], seen: datetime) -> str | None:
    family = _family(t["event_type"])
    purchased = {r[0] for r in conn.execute(
        "SELECT node_key FROM chain_nodes WHERE material_id = ? AND tier = 0 AND valid_to IS NULL", (material_id,))}
    upstream = set(t["matched_node_ids"]) - purchased
    names = _names(t)
    candidates = conn.execute(
        "SELECT * FROM events WHERE material_id = ? AND origin IN ('news', 'policy') AND last_seen_at >= ?",
        (material_id, (seen - WINDOW).isoformat(timespec="seconds"))).fetchall()
    best, best_score = None, 0.0
    for e in candidates:
        if e["event_type"] not in family:
            continue
        nodes = {r[0] for r in conn.execute("SELECT node_key FROM event_links WHERE event_id = ?", (e["id"],))}
        known = set().union(*[_names(json.loads(r[0])) for r in conn.execute(
            "SELECT t.result FROM event_articles ea JOIN triage t ON t.article_id = ea.article_id AND t.state = 'done' "
            "WHERE ea.event_id = ?", (e["id"],))])
        shared_node = bool(nodes & upstream)
        shared_name = bool(names & known)
        same_place = t["place"] != "unknown" and fold(t["place"]) == fold(e["place"])
        same_country = t["country_iso2"] != "unknown" and t["country_iso2"] == e["country_iso2"]
        score = _similar(t["title_fr"], e["title"])
        if shared_node or shared_name or same_place or (same_country and score >= TITLE_SIMILARITY):
            score += int(shared_node) + int(shared_name) + int(e["event_type"] == t["event_type"])
            if score > best_score:
                best, best_score = e["id"], score
    return best


def group_triaged(conn: sqlite3.Connection) -> list[str]:
    """Attach triaged relevant articles to events; returns the ids of the events created or updated."""
    touched: list[str] = []
    rows = conn.execute(
        "SELECT t.*, a.first_seen_at, a.kind, a.id AS aid FROM triage t JOIN articles a ON a.id = t.article_id "
        "WHERE t.state = 'done' AND a.status = 'triaged' ORDER BY a.first_seen_at, t.id").fetchall()
    for row in rows:
        t = json.loads(row["result"])
        if not t["relevant"]:
            continue
        seen = datetime.fromisoformat(row["first_seen_at"])
        event_id = _find_event(conn, row["material_id"], t, seen)
        now = utcnow()
        if event_id is None:
            event_id = _event_id(conn, seen.strftime("%Y-%m-%d"))
            conn.execute(
                "INSERT INTO events (id, material_id, event_type, event_status, origin, title, summary, place, "
                "country_iso2, first_seen_at, last_seen_at, confirmation, severity_hint, future_dates, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'early_signal', ?, ?, ?)",
                (event_id, row["material_id"], t["event_type"], t["event_status"], row["kind"], t["title_fr"],
                 t["summary_fr"], t["place"], t["country_iso2"], row["first_seen_at"], row["first_seen_at"],
                 t["severity_hint"], json.dumps(t["future_dates"], ensure_ascii=False), now),
            )
        else:
            current = conn.execute("SELECT severity_hint, future_dates FROM events WHERE id = ?",
                                   (event_id,)).fetchone()
            known = json.loads(current["future_dates"])
            dates = known + [d for d in t["future_dates"] if d not in known]
            conn.execute(
                "UPDATE events SET last_seen_at = MAX(last_seen_at, ?), event_status = ?, severity_hint = ?, "
                "future_dates = ?, updated_at = ? WHERE id = ?",
                (row["first_seen_at"], t["event_status"], max(current["severity_hint"], t["severity_hint"]),
                 json.dumps(dates, ensure_ascii=False), now, event_id),
            )
        conn.execute("INSERT OR IGNORE INTO event_articles (event_id, article_id, lineage_key, reliability) "
                     "VALUES (?, ?, '', 0)", (event_id, row["aid"]))
        _link(conn, event_id, row["material_id"], t["matched_node_ids"])
        if event_id not in touched:
            touched.append(event_id)
    if rows:
        conn.execute("UPDATE articles SET status = 'grouped' WHERE status = 'triaged' AND id IN "
                     "(SELECT article_id FROM event_articles)")
    for event_id in touched:
        refresh_corroboration(conn, event_id)
    return touched


def _measurement_severity(source: str, item: sqlite3.Row, details: dict[str, Any]) -> int:
    m = item["magnitude"]
    if source == "gdacs":
        return {"red": 3, "orange": 2, "green": 1}.get(str(details.get("alert_level", "")).lower(), 1)
    if source == "usgs_earthquakes":
        return 3 if m and m >= 7 else 2 if m and m >= 6 else 1
    if source == "imf_portwatch_chokepoints":
        return 3 if m is not None and m < 0.3 else 2
    if source == "pegelonline":
        return 2 if m is not None and m < 78 else 1
    if source == "nasa_firms":
        return 2 if m and m >= 100 else 1
    return 2


def events_from_measurements(conn: sqlite3.Connection) -> list[str]:
    """Hazard and logistics items become events directly, linked to the nodes whose rules they triggered."""
    created: list[str] = []
    rules = {r["id"]: r for r in conn.execute("SELECT id, material_id, node_key FROM watch_rules")}
    for item in conn.execute("SELECT * FROM articles WHERE status = 'new' AND kind IN ('hazard', 'logistics') "
                             "ORDER BY id").fetchall():
        details = json.loads(item["details"])
        by_material: dict[str, list[str]] = {}
        for rid in json.loads(item["rules_hit"]):
            r = rules.get(rid)
            if r is not None:
                by_material.setdefault(r["material_id"], [])
                if r["node_key"]:
                    by_material[r["material_id"]].append(r["node_key"])
        for material_id, nodes in by_material.items():
            seen = datetime.fromisoformat(item["first_seen_at"])
            event_id = _event_id(conn, seen.strftime("%Y-%m-%d"))
            event_type = (HAZARD_TYPES.get(str(details.get("event_type", "")), "natural_disaster")
                          if item["kind"] == "hazard" else
                          "maritime_disruption" if item["source"] == "imf_portwatch_chokepoints"
                          else "infrastructure_failure")
            conn.execute(
                "INSERT INTO events (id, material_id, event_type, event_status, origin, title, place, country_iso2, "
                "lat, lon, first_seen_at, last_seen_at, confirmation, severity_hint, updated_at) "
                "VALUES (?, ?, ?, 'ongoing', ?, ?, ?, 'unknown', ?, ?, ?, ?, 'early_signal', ?, ?)",
                (event_id, material_id, event_type, item["kind"], item["title"], str(details.get("country", ""))[:80],
                 item["lat"], item["lon"], item["first_seen_at"], item["first_seen_at"],
                 _measurement_severity(item["source"], item, details), utcnow()),
            )
            conn.execute("INSERT INTO event_articles (event_id, article_id, lineage_key, reliability) "
                         "VALUES (?, ?, '', 0)", (event_id, item["id"]))
            _link(conn, event_id, material_id, sorted(set(nodes)))
            refresh_corroboration(conn, event_id)
            created.append(event_id)
        conn.execute("UPDATE articles SET status = 'grouped' WHERE id = ?", (item["id"],))
    return created
