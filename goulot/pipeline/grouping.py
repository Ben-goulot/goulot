"""Events (docs/SPEC.md, section 4, steps 4 and 5).

- Triaged relevant articles join an event of the same material within 7 days: a type of the same family (the
  triage may call one fire an outage or an infrastructure failure), then a shared upstream node, the same place or a
  shared company, site or port, or else the same country and similar titles; otherwise they open a new one. The
  purchased material itself is matched by nearly every article: it never links two articles alone.
- Hazard and logistics items come from official measurements (GDACS, USGS, NOAA, PortWatch, PEGELONLINE): the
  code turns them into events without AI. A reading of the same source at the same place within 7 days prolongs the
  ongoing event instead of opening a new one.
- Decision of 8 November 2026, only what is really linked: a satellite fire detection never opens an event alone
  (crop burning and flares around a site); within 5 km of the site and not a recurring heat source, it joins an
  event the press reports there within 3 days. A GDACS drought opens none (its effects reach a purchase through
  river levels and the press). The Kaub gauge counts fully for Rhine sites upstream of Kaub, the barges' bottleneck;
  for a site downstream (Cologne, Rotterdam) only at the critical level.
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
FIRE_SITE_KM = 5.0  # a satellite hot spot farther from the site is a field or another plant
FIRE_WAIT = timedelta(days=3)  # how long a hot spot waits for the press to report a fire at the site
FLARE_LOOKBACK, FLARE_DAYS, FLARE_KM = timedelta(days=30), 3, 1.0  # seen on 3 days of 30 within 1 km: a flare
KAUB_LAT = 50.08  # Kaub gauge; the Rhine flows north: a site south of it is upstream
KAUB_CRITICAL_CM = 78  # below, barges are severely limited on the whole Rhine
SITE_NODE_TYPES = ("mine", "plant", "recycler", "company", "port", "distributor")
HAZARD_TYPES = {"IND": "industrial_outage", "LS": "natural_disaster", "TS": "natural_disaster",
                "FL": "natural_disaster", "EQ": "natural_disaster", "VO": "natural_disaster",
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


SAME_PLACE_DEG = 0.25  # about 25 km: the same station, strait or fire area


def _same_measurement(conn: sqlite3.Connection, material_id: str, item: sqlite3.Row) -> str | None:
    """An event of this material, still ongoing and seen within 7 days, made of readings of the same source at the
    same place (the Kaub gauge each day, a strait's traffic each week). Its next reading prolongs it instead of
    opening a new event, so it is analysed again only when it gets worse."""
    since = (datetime.fromisoformat(item["first_seen_at"]) - WINDOW).isoformat()
    for e in conn.execute(
            "SELECT DISTINCT e.id, a.lat, a.lon, a.title FROM events e JOIN event_articles ea ON ea.event_id = e.id "
            "JOIN articles a ON a.id = ea.article_id WHERE e.material_id = ? AND e.origin = ? AND a.source = ? "
            "AND e.event_status != 'resolved' AND e.last_seen_at >= ? ORDER BY e.last_seen_at DESC",
            (material_id, item["kind"], item["source"], since)):
        if item["lat"] is None or e["lat"] is None:
            if item["lat"] is None and e["lat"] is None and _station(e["title"]) == _station(item["title"]):
                return str(e["id"])
            continue
        if abs(e["lat"] - item["lat"]) <= SAME_PLACE_DEG and abs(e["lon"] - item["lon"]) <= SAME_PLACE_DEG:
            return str(e["id"])
    return None


def _station(title: str) -> str:
    """A reading's title without its figures (« Basses eaux : KAUB à 74 cm » → « basses eaux : kaub à cm »)."""
    return re.sub(r"[\d.,]+", "", title).strip().lower()


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from goulot.chain.leadtimes import great_circle_km

    return great_circle_km(lat1, lon1, lat2, lon2)


def _recurring_heat(conn: sqlite3.Connection, item: sqlite3.Row) -> bool:
    """A hot spot seen at the same place on several days of the last month: a flare or a furnace, not a fire."""
    seen = datetime.fromisoformat(item["first_seen_at"])
    days = set()
    for r in conn.execute("SELECT lat, lon, first_seen_at FROM articles WHERE source = 'nasa_firms' AND id != ? "
                          "AND first_seen_at >= ? AND first_seen_at <= ? AND lat BETWEEN ? AND ? "
                          "AND lon BETWEEN ? AND ?",
                          (item["id"], (seen - FLARE_LOOKBACK).isoformat(), item["first_seen_at"],
                           item["lat"] - 0.02, item["lat"] + 0.02, item["lon"] - 0.03, item["lon"] + 0.03)):
        if _distance_km(item["lat"], item["lon"], r["lat"], r["lon"]) <= FLARE_KM:
            days.add(r["first_seen_at"][:10])
    days.add(item["first_seen_at"][:10])
    return len(days) >= FLARE_DAYS


def _fire_joins_press(conn: sqlite3.Connection, item: sqlite3.Row, by_material: dict[str, list[str]],
                      sites: dict[tuple[str, str], tuple[float, float]]) -> bool:
    """Attach a satellite hot spot to the press events of the site it lies on. Returns True when it joined one."""
    seen = datetime.fromisoformat(item["first_seen_at"])
    joined = False
    for material_id, nodes in by_material.items():
        near = [k for k in nodes if (material_id, k) in sites
                and _distance_km(item["lat"], item["lon"], *sites[(material_id, k)]) <= FIRE_SITE_KM]
        if not near:
            continue
        marks = ",".join("?" * len(near))
        for e in conn.execute(
                f"SELECT DISTINCT e.id, e.event_type FROM events e JOIN event_links l ON l.event_id = e.id "  # noqa: S608
                f"WHERE e.material_id = ? AND e.origin = 'news' AND l.node_key IN ({marks}) AND e.last_seen_at >= ? "
                f"AND e.first_seen_at <= ?",
                (material_id, *near, (seen - FIRE_WAIT).isoformat(), (seen + FIRE_WAIT).isoformat())):
            if e["event_type"] not in TYPE_FAMILIES[0]:  # a fire tells nothing about a strike or a sanction
                continue
            conn.execute("INSERT OR IGNORE INTO event_articles (event_id, article_id, lineage_key, reliability) "
                         "VALUES (?, ?, '', 0)", (e["id"], item["id"]))
            refresh_corroboration(conn, e["id"])
            joined = True
    return joined


def _kaub_counts(conn: sqlite3.Connection, material_id: str, nodes: list[str], level: float | None) -> bool:
    """The Kaub gauge for a material: always when a site next to its Rhine node lies upstream of Kaub; for sites
    downstream only, at the critical level."""
    if level is not None and level < KAUB_CRITICAL_CM:
        return True
    marks = ",".join("?" * len(nodes)) or "''"
    neighbours = conn.execute(
        f"SELECT n.lat FROM chain_edges ed JOIN chain_nodes n ON n.material_id = ed.material_id AND n.valid_to IS NULL "  # noqa: S608
        f"AND n.node_key = CASE WHEN ed.from_key IN ({marks}) THEN ed.to_key ELSE ed.from_key END "
        f"WHERE ed.material_id = ? AND ed.valid_to IS NULL AND (ed.from_key IN ({marks}) OR ed.to_key IN ({marks})) "
        f"AND n.node_type IN ({','.join('?' * len(SITE_NODE_TYPES))}) AND n.lat IS NOT NULL",
        (*nodes, material_id, *nodes, *nodes, *SITE_NODE_TYPES)).fetchall()
    return any(r["lat"] < KAUB_LAT for r in neighbours)


def events_from_measurements(conn: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    """Hazard and logistics items become events directly, linked to the nodes whose rules they triggered."""
    created: list[str] = []
    now = now or datetime.fromisoformat(utcnow())
    rules = {r["id"]: r for r in conn.execute("SELECT id, material_id, node_key FROM watch_rules")}
    sites = {(r["material_id"], r["node_key"]): (r["lat"], r["lon"]) for r in conn.execute(
        "SELECT material_id, node_key, lat, lon FROM watch_rules WHERE active = 1 AND kind = 'zone' "
        "AND lat IS NOT NULL AND lon IS NOT NULL")}
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
        for material_id in json.loads(item["materials"]):
            by_material.setdefault(material_id, [])  # found near a site of Ma chaîne only: no public link
        if item["source"] == "nasa_firms":
            if item["lat"] is None or _recurring_heat(conn, item):
                conn.execute("UPDATE articles SET status = 'irrelevant' WHERE id = ?", (item["id"],))
            elif _fire_joins_press(conn, item, by_material, sites):
                conn.execute("UPDATE articles SET status = 'grouped' WHERE id = ?", (item["id"],))
            elif now - datetime.fromisoformat(item["first_seen_at"]) > FIRE_WAIT:
                conn.execute("UPDATE articles SET status = 'irrelevant' WHERE id = ?", (item["id"],))
            continue  # otherwise it waits, still « new », for the press of the next cycles
        if item["source"] == "gdacs" and str(details.get("event_type", "")) == "DR":
            conn.execute("UPDATE articles SET status = 'irrelevant' WHERE id = ?", (item["id"],))
            continue
        if item["source"] == "pegelonline" and details.get("station") == "KAUB":
            by_material = {m: n for m, n in by_material.items() if _kaub_counts(conn, m, n, item["magnitude"])}
        for material_id, nodes in by_material.items():
            severity = _measurement_severity(item["source"], item, details)
            ongoing = _same_measurement(conn, material_id, item)
            if ongoing is not None:  # the same reading again: it prolongs the event (decision of 6 November 2026)
                conn.execute("UPDATE events SET last_seen_at = ?, title = ?, severity_hint = MAX(severity_hint, ?), "
                             "updated_at = ? WHERE id = ?",
                             (item["first_seen_at"], item["title"], severity, utcnow(), ongoing))
                conn.execute("INSERT OR IGNORE INTO event_articles (event_id, article_id, lineage_key, reliability) "
                             "VALUES (?, ?, '', 0)", (ongoing, item["id"]))
                _link(conn, ongoing, material_id, sorted(set(nodes)))
                refresh_corroboration(conn, ongoing)
                continue
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
                 item["lat"], item["lon"], item["first_seen_at"], item["first_seen_at"], severity, utcnow()),
            )
            conn.execute("INSERT INTO event_articles (event_id, article_id, lineage_key, reliability) "
                         "VALUES (?, ?, '', 0)", (event_id, item["id"]))
            _link(conn, event_id, material_id, sorted(set(nodes)))
            refresh_corroboration(conn, event_id)
            created.append(event_id)
        conn.execute("UPDATE articles SET status = 'grouped' WHERE id = ?", (item["id"],))
    return created
