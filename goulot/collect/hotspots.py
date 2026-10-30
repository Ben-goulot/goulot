"""World hot spots (docs/SPEC.md, section 8, « Zones d'alerte »): chokepoints whose traffic collapsed (IMF
PortWatch), chokepoints the general press still reports in crisis (Google News), orange or red GDACS alerts and the
Copernicus Rapid Mapping activations of the last 30 days.
Shown on every map, masked at will; they never change a verdict by themselves: a flow or a node of the chain inside
one is only shown as exposed.

Traffic drop: the mean of the last 7 days under 50 % of the usual traffic, the median of the daily transits over the
3 years before the last 90 days (docs/DECISIONS.md, 27 October 2026). A one-year median would miss a long closure:
Hormuz, nearly closed since March 2026, would have made its own low traffic the norm. A crisis that lasts longer
than about 18 months becomes the norm in turn (Suez since late 2023): the press check covers it. A strait is reported
in crisis when, over 7 days, at least 3 headlines name it with a crisis word, from at least 2 publishers, one of them
reliable (0.7 or more, config/reliability.yaml): a headline is counted, never interpreted, and no AI is involved.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2

from goulot.chain import geo, places
from goulot.chain.countries import name_fr
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
GDACS_RADIUS_KM = {"EQ": 100, "TC": 300, "FL": 100, "VO": 50, "WF": 50, "DR": 300, "TS": 150, "IND": 30, "LS": 30,
                   "OT": 50}
GDACS_LEVELS = ("orange", "red")
AREA_KINDS = {"DR": "Sécheresse", "FL": "Inondations", "WF": "Incendies"}
PORTWATCH_PAGE_URL = "https://portwatch.imf.org/pages/chokepoints"
NEWS_STRAITS = ("hormuz", "bab_el_mandeb", "suez", "malacca", "panama", "bosporus", "gibraltar", "taiwan", "kerch",
                "dover", "oresund", "cape_good_hope")
NEWS_DAYS = 7
MEDIA_PREFIX = "media_"  # ids of the European media feeds in config/sources.yaml
NEWS_MIN_HEADLINES = 3
NEWS_MIN_PUBLISHERS = 2
NEWS_MIN_RELIABILITY = 0.7
CRISIS_WORDS = frozenset({
    "attack", "attacks", "attacked", "closed", "closure", "closes", "blockade", "blocked", "disruption", "disrupted",
    "suspended", "suspends", "seized", "seizure", "mine", "mines", "missile", "missiles", "drone", "drones",
    "crisis", "halt", "halted", "diverted", "diversions", "war", "strike", "strikes", "explosion", "explosions",
    "attaque", "attaques", "ferme", "fermeture", "blocus", "bloque", "perturbation", "perturbations", "suspendu",
    "suspendue", "saisi", "crise", "frappe", "frappes", "guerre", "detournes",
    # German, Spanish and Italian headlines of the European media feeds
    "angriff", "angriffe", "gesperrt", "sperrung", "krise", "krieg", "beschuss",
    "ataque", "ataques", "bloqueo", "cierre", "cerrado", "guerra",
    "attacco", "attacchi", "blocco", "chiusura", "chiuso", "crisi",
})


def news_queries() -> list[tuple[str, str, str]]:
    """(query, language, country): one English and one French query naming the watched straits."""
    names_en = [places.chokepoints()[s]["portwatch_name"] for s in NEWS_STRAITS]
    names_fr = [places.chokepoints()[s]["name_fr"] for s in NEWS_STRAITS]
    def query(names: list[str]) -> str:
        return "(" + " OR ".join(f'"{n}"' for n in names) + ") when:7d"

    return [(query(names_en), "en", "US"), (query(names_fr), "fr", "FR")]


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


def news_hotspots(items: list[Item], now: datetime) -> list[dict[str, Any]]:
    from goulot.collect.rules import _names_strait
    from goulot.pipeline.corroborate import domain_reliability

    recent = [it for it in items if it.published_at is None or now - it.published_at <= timedelta(days=NEWS_DAYS)]
    out = []
    for strait in NEWS_STRAITS:
        c = places.chokepoints()[strait]
        found: dict[str, Item] = {}
        for it in recent:
            text = fold(it.title)
            words = set(text.replace("'", " ").split())
            if words & CRISIS_WORDS and any(_names_strait(text, n) for n in c["names"]):
                found.setdefault(fold(it.title), it)  # the same headline twice counts once
        publishers = {(it.provider_domain or it.domain) for it in found.values()} - {""}
        if len(found) < NEWS_MIN_HEADLINES or len(publishers) < NEWS_MIN_PUBLISHERS:
            continue
        best = max(found.values(), key=lambda it: domain_reliability(it.provider_domain or it.domain))
        if domain_reliability(best.provider_domain or best.domain) < NEWS_MIN_RELIABILITY:
            continue
        short = c["name_fr"].replace("Détroit d'", "").replace("Détroit de ", "").replace("Détroit du ", "")
        first = min((it.published_at for it in found.values() if it.published_at), default=now)
        out.append({
            "id": f"strait_news:{strait}", "kind": "strait_news",
            "label": f"{short} : crise signalée par la presse ({len(found)} titres)"[:60],
            "status": "en cours", "lat": c["lat"], "lon": c["lon"],
            "rings": [places.circle(c["lat"], c["lon"], c["radius_km"])],
            "source_url": best.url if best.url.startswith("https://") else "",
            "source_name": best.provider_domain or best.domain, "started_at": first.date().isoformat(),
        })
    return out


def country_rings(iso2_codes: list[str]) -> list[list[list[float]]]:
    """Outlines of these countries (embedded Natural Earth borders)."""
    wanted = set(iso2_codes)
    return [ring for c in geo._borders() or [] if c["iso2"] in wanted for ring in c["polygons"]]


def area_label(kind: str, iso2_codes: list[str]) -> str:
    names = sorted(name_fr(c) for c in iso2_codes)
    return (f"{AREA_KINDS.get(kind, 'Alerte')} : {len(names)} pays ({', '.join(names[:2])}"
            f"{'…' if len(names) > 2 else ''})")


def gdacs_hotspots(items: list[Item]) -> list[dict[str, Any]]:
    """An event spread over several countries covers their outlines: its only point is a mere centre. Without any
    recognized country, it is left out rather than drawn at the wrong place."""
    out = []
    for it in items:
        level = str(it.details.get("alert_level", "")).lower()
        kind = str(it.details.get("event_type", "")).upper()
        if level not in GDACS_LEVELS or it.lat is None or it.lon is None:
            continue
        label, rings = clean_text(it.title, 60), [places.circle(it.lat, it.lon, GDACS_RADIUS_KM.get(kind, 100))]
        if it.details.get("area_wide"):
            codes = [str(c) for c in it.details.get("countries", [])]
            label, rings = area_label(kind, codes), country_rings(codes)
            if not rings:
                continue
        out.append({
            "id": f"gdacs:{it.identity()[:16]}", "kind": "gdacs", "label": label,
            "status": "alerte rouge" if level == "red" else "alerte orange", "lat": it.lat, "lon": it.lon,
            "rings": rings,
            "source_url": it.url if it.url.startswith("https://") else "", "source_name": "GDACS",
            "started_at": (it.published_at or datetime.now(UTC)).date().isoformat(),
        })
    return out


def copernicus_hotspots(items: list[Item]) -> list[dict[str, Any]]:
    """Copernicus Rapid Mapping activations of the last 30 days: a circle around each zone's centre."""
    out = []
    for it in items:
        if it.lat is None or it.lon is None or not it.key:
            continue
        kind = str(it.details.get("event_type", "OT"))
        out.append({
            "id": f"copernicus:{it.key.removeprefix('cems:')}", "kind": "copernicus", "label": clean_text(it.title, 60),
            "status": "activation Copernicus", "lat": it.lat, "lon": it.lon,
            "rings": [places.circle(it.lat, it.lon, GDACS_RADIUS_KM.get(kind, 50))],
            "source_url": it.url, "source_name": sources.COPERNICUS_CREDIT,
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
    """After the collection: GDACS hot spots from this cycle's answer, straits in crisis in the press (two Google
    News queries, plus the headlines of the European media feeds read by the cycle), traffic drops from the stored
    history (updated once a day). A failure keeps the previous hot spots: they are a display, never a verdict."""
    with db.transaction() as conn:
        if "gdacs" in fetched:
            _replace(conn, "gdacs", gdacs_hotspots(fetched["gdacs"]), now)
        if "copernicus_ems" in fetched:
            _replace(conn, "copernicus", copernicus_hotspots(fetched["copernicus_ems"]), now)
    # European media feeds read by this cycle add their headlines, without a request of their own.
    headlines = [it for source_id, items in fetched.items() if source_id.startswith(MEDIA_PREFIX) for it in items]
    complete = True
    if enabled("google_news_rss"):
        try:
            headlines += [it for q, lang, cc in news_queries() for it in sources.google_news(http, q, lang, cc)]
        except (httpx2.HTTPError, sources.SourceError, ValueError) as exc:
            complete = False  # the previous count stays: fewer headlines would only hide a crisis
            log.warning("hotspots: news check failed: %s", type(exc).__name__)
    if complete and (headlines or enabled("google_news_rss")):
        with db.transaction() as conn:
            _replace(conn, "strait_news", news_hotspots(headlines, now), now)
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
