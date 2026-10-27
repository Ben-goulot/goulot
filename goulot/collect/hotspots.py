"""World hot spots (docs/SPEC.md, section 8, « Zones d'alerte »): chokepoints whose traffic collapsed (IMF
PortWatch) and orange or red GDACS alerts. Shown on every map, masked at will; they never change a verdict by
themselves: a flow or a node of the chain inside one is only shown as exposed.

Traffic drop: the mean of the last 7 days under 50 % of the usual traffic, the median of the daily transits over the
3 years before the last 90 days (docs/DECISIONS.md, 27 October 2026). A one-year median would miss a long closure:
Hormuz, nearly closed since March 2026, would have made its own low traffic the norm.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2

from goulot.chain import places
from goulot.collect import sources
from goulot.collect.base import Item, clean_text, enabled, fold, iso, parse_datetime
from goulot.db.connection import Database
from goulot.net.client import fetch

log = logging.getLogger("goulot.collect")

DROP_RATIO = 0.5
RECENT_DAYS = 7
BASELINE_YEARS = 3
BASELINE_GAP_DAYS = 90
MIN_BASELINE_DAYS = 180
KEEP_YEARS = 4
PAGE = 2000
MAX_PAGES = 30
GDACS_RADIUS_KM = {"EQ": 100, "TC": 300, "FL": 100, "VO": 50, "WF": 50, "DR": 300, "TS": 150}
GDACS_LEVELS = ("orange", "red")
PORTWATCH_PAGE_URL = "https://portwatch.imf.org/pages/chokepoints"


def fetch_traffic(client: httpx2.Client, since: date) -> list[tuple[str, str, str, float]]:
    """(portid, portname, YYYY-MM-DD, transits) since a day, page by page."""
    out: list[tuple[str, str, str, float]] = []
    for page in range(MAX_PAGES):
        result = fetch(client, sources.PORTWATCH, params={
            "where": f"date >= DATE '{since.isoformat()}'", "outFields": "date,portid,portname,n_total",
            "orderByFields": "date,portid", "resultOffset": str(page * PAGE), "resultRecordCount": str(PAGE),
            "f": "json"})
        if result.status != 200:
            raise sources.SourceError(f"HTTP {result.status}")
        try:
            data = json.loads(result.content)
        except json.JSONDecodeError as exc:
            raise sources.SourceError("invalid JSON") from exc
        features = (data or {}).get("features", []) or []
        for feat in features:
            a = feat.get("attributes") or {}
            day, total = parse_datetime(a.get("date")), a.get("n_total")
            pid = str(a.get("portid") or "")[:40]
            if pid and day and isinstance(total, int | float):
                out.append((pid, clean_text(str(a.get("portname") or pid), 80), day.date().isoformat(), float(total)))
        if len(features) < PAGE and not (data or {}).get("exceededTransferLimit"):
            break
    return out


def update_traffic(conn: sqlite3.Connection, client: httpx2.Client, now: datetime) -> int:
    """Daily: the days since the last one stored (3 years and 3 months the first time). Returns rows written."""
    last = conn.execute("SELECT MAX(day) FROM chokepoint_traffic").fetchone()[0]
    if last and (now.date() - date.fromisoformat(last)).days < 1:
        return 0
    since = (date.fromisoformat(last) - timedelta(days=7) if last
             else now.date() - timedelta(days=365 * BASELINE_YEARS + BASELINE_GAP_DAYS + 30))
    rows = fetch_traffic(client, since)
    conn.executemany("INSERT OR REPLACE INTO chokepoint_traffic VALUES (?, ?, ?, ?)", rows)
    conn.execute("DELETE FROM chokepoint_traffic WHERE day < ?",
                 ((now.date() - timedelta(days=365 * KEEP_YEARS)).isoformat(),))
    return len(rows)


def traffic_drops(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Chokepoints whose last week runs under DROP_RATIO of their usual traffic."""
    out = []
    for pid, name in conn.execute("SELECT DISTINCT portid, portname FROM chokepoint_traffic ORDER BY portid"):
        days = conn.execute("SELECT day, n_total FROM chokepoint_traffic WHERE portid = ? ORDER BY day",
                            (pid,)).fetchall()
        if len(days) < RECENT_DAYS:
            continue
        latest = date.fromisoformat(days[-1][0])
        start = (latest - timedelta(days=365 * BASELINE_YEARS + BASELINE_GAP_DAYS)).isoformat()
        end = (latest - timedelta(days=BASELINE_GAP_DAYS)).isoformat()
        baseline_days = [n for d, n in days if start <= d < end]
        if len(baseline_days) < MIN_BASELINE_DAYS:
            continue
        usual = statistics.median(baseline_days)
        recent = [n for _, n in days[-RECENT_DAYS:]]
        level = sum(recent) / len(recent)
        if usual <= 0 or level >= DROP_RATIO * usual:
            continue
        since = days[-1][0]
        for d, n in reversed(days):  # first day of the current run under the threshold
            if n >= DROP_RATIO * usual:
                break
            since = d
        out.append({"portid": pid, "portname": name, "ratio": level / usual, "usual": usual, "since": since,
                    "latest": days[-1][0]})
    return out


def _by_portwatch_name(name: str) -> dict[str, Any] | None:
    wanted = fold(name)
    found = next((c for c in places.chokepoints().values() if fold(c["portwatch_name"]) == wanted), None)
    return found or places.match_chokepoint(name)


def strait_hotspots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for drop in traffic_drops(conn):
        c = _by_portwatch_name(drop["portname"])
        if c is None:
            continue
        short = c["name_fr"].replace("Détroit d'", "").replace("Détroit de ", "").replace("Détroit du ", "")
        out.append({
            "id": f"strait_traffic:{c['id']}", "kind": "strait_traffic",
            "label": f"{short} : trafic à {drop['ratio'] * 100:.0f} % de la normale"[:60],
            "status": "en cours", "lat": c["lat"], "lon": c["lon"],
            "rings": [places.circle(c["lat"], c["lon"], c["radius_km"])],
            "source_url": PORTWATCH_PAGE_URL, "source_name": "IMF PortWatch", "started_at": drop["since"],
        })
    return out


def gdacs_hotspots(items: list[Item]) -> list[dict[str, Any]]:
    out = []
    for it in items:
        level = str(it.details.get("alert_level", "")).lower()
        kind = str(it.details.get("event_type", "")).upper()
        if level not in GDACS_LEVELS or it.lat is None or it.lon is None:
            continue
        out.append({
            "id": f"gdacs:{it.identity()[:16]}", "kind": "gdacs", "label": clean_text(it.title, 60),
            "status": "alerte rouge" if level == "red" else "alerte orange", "lat": it.lat, "lon": it.lon,
            "rings": [places.circle(it.lat, it.lon, GDACS_RADIUS_KM.get(kind, 100))],
            "source_url": it.url if it.url.startswith("https://") else "", "source_name": "GDACS",
            "started_at": (it.published_at or datetime.now(UTC)).date().isoformat(),
        })
    return out


def _replace(conn: sqlite3.Connection, kind: str, spots: list[dict[str, Any]], now: datetime) -> None:
    conn.execute("DELETE FROM hotspots WHERE kind = ?", (kind,))
    conn.executemany(
        "INSERT OR REPLACE INTO hotspots (id, kind, label, status, lat, lon, rings, source_url, source_name, "
        "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(s["id"], s["kind"], s["label"], s["status"], s["lat"], s["lon"], json.dumps(s["rings"]),
          s["source_url"], s["source_name"], s["started_at"], iso(now)) for s in spots])


def refresh(db: Database, http: httpx2.Client, now: datetime, fetched: dict[str, list[Item]]) -> int:
    """After the collection: GDACS hot spots from this cycle's answer, traffic drops from the stored history (updated
    once a day). A failure keeps the previous hot spots: they are a display, never a verdict."""
    with db.transaction() as conn:
        if "gdacs" in fetched:
            _replace(conn, "gdacs", gdacs_hotspots(fetched["gdacs"]), now)
    if enabled("imf_portwatch_chokepoints"):
        try:
            with db.transaction() as conn:
                update_traffic(conn, http, now)
                _replace(conn, "strait_traffic", strait_hotspots(conn), now)
        except (httpx2.HTTPError, sources.SourceError, ValueError) as exc:
            log.warning("hotspots: portwatch history failed: %s", type(exc).__name__)
    with db.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM hotspots").fetchone()[0])


def current(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [{**dict(r), "rings": json.loads(r["rings"])}
            for r in conn.execute("SELECT * FROM hotspots ORDER BY kind, id")]
