"""A collection cycle: every due source, the rule filter, storage, run records (docs/SPEC.md, section 4).

Every 3 hours, and at start-up to catch up after the Mac slept: each source resumes from its last success
(minus one hour of overlap), within what the source keeps. « Rien à signaler » needs a successful cycle less than
12 hours old (`freshness`).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx2

from goulot.chain import store
from goulot.collect import sources
from goulot.collect.base import Item, enabled, iso, load_sources
from goulot.collect.rules import Rule, active_rules, match, material_names
from goulot.db.connection import Database

if TYPE_CHECKING:
    from goulot.private.watch import PrivateWatch

log = logging.getLogger("goulot.collect")

FRESHNESS_LIMIT = timedelta(hours=12)
FIRST_LOOKBACK = timedelta(days=3)
OVERLAP = timedelta(hours=1)
DAILY = timedelta(hours=20)
DISRUPTION_TERMS = ('strike OR fire OR explosion OR shutdown OR outage OR "force majeure" OR flood OR sanctions '
                    'OR "export ban" OR "export controls" OR closure')
REQUIRED_FAMILIES = ("news", "hazard")
MAX_ITEMS_PER_SOURCE = 1_000  # bounded queue: a flooding feed cannot fill the database nor the triage
BACKOFF_BASE = timedelta(hours=3)
BACKOFF_MAX = timedelta(hours=24)
SITES_PER_QUERY = 8

SecretGetter = Callable[[str], str | None]


@dataclass
class CycleResult:
    cycle_id: str
    ok: bool
    fetched: int = 0
    kept: int = 0
    failed: list[str] = field(default_factory=list)


def _q(term: str) -> str:
    term = term.replace('"', "")
    return f'"{term}"' if " " in term or "-" in term else term


def queries(conn: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    """(query, material ids) for the news sources: one per material on its names, and one per group of sites
    combined with disruption terms. Built from the stored public rules only."""
    out: list[tuple[str, list[str]]] = []
    for m in store.list_materials(conn):
        if m["status"] != "validated":
            continue
        material = store.get_material(conn, m["id"]) or {}
        names = material_names(material)
        if names:
            out.append(("(" + " OR ".join(_q(n) for n in names) + ")", [m["id"]]))
        sites = [r["value"] for r in conn.execute(
            "SELECT value FROM watch_rules WHERE material_id = ? AND active = 1 AND kind = 'keyword' "
            "AND node_key IS NOT NULL AND node_key IN (SELECT node_key FROM chain_nodes WHERE material_id = ? "
            "AND valid_to IS NULL AND node_type != 'country')", (m["id"], m["id"]))]
        for i in range(0, len(sites), SITES_PER_QUERY):
            group = sites[i:i + SITES_PER_QUERY]
            out.append(("(" + " OR ".join(_q(s) for s in group) + f") ({DISRUPTION_TERMS})", [m["id"]]))
    return out


def _last_success(conn: sqlite3.Connection, source: str) -> datetime | None:
    row = conn.execute("SELECT MAX(started_at) FROM collect_runs WHERE source = ? AND ok = 1", (source,)).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def _store(conn: sqlite3.Connection, item: Item, hits: list[Rule], now: datetime) -> bool:
    """Only public rules are kept with the article: a rule from Ma chaîne (negative id, in memory) leaves no trace."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO articles (source, kind, url, url_hash, domain, provider_domain, title, lang, "
        "published_at, first_seen_at, collected_at, excerpt, lat, lon, magnitude, details, rules_hit, materials) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item.source, item.kind, item.url, item.identity(), item.domain, item.provider_domain, item.title,
         item.lang, iso(item.published_at), iso(now), iso(now), item.excerpt, item.lat, item.lon, item.magnitude,
         json.dumps(item.details, ensure_ascii=False), json.dumps(sorted({r.id for r in hits if r.id > 0})),
         json.dumps(sorted({r.material_id for r in hits}))),
    )
    return cur.rowcount == 1


def _fetchers(conn: sqlite3.Connection, now: datetime, secret: SecretGetter,
              private_queries: list[tuple[str, list[str]]] | None = None
              ) -> dict[str, Callable[[httpx2.Client, datetime], list[Item]]]:
    qs = queries(conn) + (private_queries or [])
    boxes = [(r["node_key"], r["lat"], r["lon"]) for r in conn.execute(
        "SELECT DISTINCT node_key, lat, lon FROM watch_rules WHERE active = 1 AND kind = 'zone'")]
    terms = sorted({r["value"] for r in conn.execute(
        "SELECT value FROM watch_rules WHERE active = 1 AND kind = 'keyword' AND node_key IS NULL")})

    def news_gdelt(c: httpx2.Client, since: datetime) -> list[Item]:
        return [it for q, _ in qs for it in sources.gdelt(c, q, since, now)]

    def news_google(c: httpx2.Client, since: datetime) -> list[Item]:
        return [it for q, _ in qs for lang, cc in (("en", "US"), ("fr", "FR"))
                for it in sources.google_news(c, f"{q} when:7d", lang, cc)]

    def fires(c: httpx2.Client, since: datetime) -> list[Item]:
        key = secret("nasa_firms")
        if not key:
            raise sources.SourceError("missing key")
        return sources.firms(c, key, boxes)

    return {
        "gdelt_doc": news_gdelt,
        "google_news_rss": news_google,
        "gdacs": lambda c, s: sources.gdacs(c),
        "usgs_earthquakes": lambda c, s: sources.usgs_quakes(c),
        "nasa_firms": fires,
        "noaa_nhc": lambda c, s: sources.nhc(c),
        "imf_portwatch_chokepoints": lambda c, s: sources.portwatch(c, now),
        "pegelonline": lambda c, s: sources.pegel(c),
        "federal_register": lambda c, s: sources.federal_register(c, terms, s) if terms else [],
    }


def _backoff(conn: sqlite3.Connection, source: str, now: datetime) -> bool:
    """True while a failing source must wait: 3 h after one failure, then doubling, at most 24 h."""
    runs = conn.execute("SELECT ok, started_at FROM collect_runs WHERE source = ? ORDER BY id DESC LIMIT 8",
                        (source,)).fetchall()
    failures = 0
    for r in runs:
        if r["ok"]:
            break
        failures += 1
    if failures < 2:
        return False
    wait = min(BACKOFF_BASE * 2 ** (failures - 2), BACKOFF_MAX)
    return bool(now - datetime.fromisoformat(runs[0]["started_at"]) < wait)


def _due(conn: sqlite3.Connection, source: str, now: datetime) -> bool:
    if _backoff(conn, source, now):
        return False
    last = _last_success(conn, source)
    if last is None:
        return True
    frequency = str(load_sources().get(source, {}).get("frequency", "3h"))
    return now - last >= (DAILY if frequency.startswith("daily") else timedelta(hours=2, minutes=45))


def run_cycle(db: Database, http: httpx2.Client, secret: SecretGetter, now: datetime | None = None,
              private: PrivateWatch | None = None) -> CycleResult:
    """`private`: rules and queries of Ma chaîne, built in memory by the caller; never stored nor logged."""
    now = now or datetime.now(UTC)
    cycle_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute("INSERT INTO cycles (id, started_at) VALUES (?, ?)", (cycle_id, iso(now)))
        fetchers = _fetchers(conn, now, secret, private.queries if private else None)
        rules = active_rules(conn) + (private.rules if private else [])
        country_nodes = {r[0] for r in conn.execute(
            "SELECT node_key FROM chain_nodes WHERE valid_to IS NULL AND node_type = 'country'")}
        country_nodes |= private.needs_material if private else set()
    result = CycleResult(cycle_id, ok=False)
    families_ok: dict[str, bool] = {}
    for source_id, fetch in fetchers.items():
        if not enabled(source_id):
            continue
        family = str(load_sources()[source_id].get("family", ""))
        with db.connect() as conn:
            if not _due(conn, source_id, now):
                families_ok.setdefault(family, True)
                continue
            since = (_last_success(conn, source_id) or now - FIRST_LOOKBACK) - OVERLAP
        started, error, fetched, kept = iso(now), "", 0, 0
        try:
            items = fetch(http, since)[:MAX_ITEMS_PER_SOURCE]
            fetched = len(items)
            with db.transaction() as conn:
                for item in items:
                    hits = match(item, rules, {k: k for k in country_nodes})
                    if hits and _store(conn, item, hits, now):
                        kept += 1
        except (httpx2.HTTPError, sources.SourceError, ValueError) as exc:
            error = type(exc).__name__ + (f": {exc}" if isinstance(exc, sources.SourceError) else "")
            log.warning("source failed: source=%s error=%s", source_id, error)
        ok = not error
        families_ok[family] = families_ok.get(family, False) or ok
        if not ok:
            result.failed.append(source_id)
        result.fetched += fetched
        result.kept += kept
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO collect_runs (cycle_id, source, family, started_at, finished_at, ok, error, fetched, kept)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cycle_id, source_id, family, started, iso(datetime.now(UTC)), int(ok), error[:120], fetched, kept),
            )
    result.ok = all(families_ok.get(f, False) for f in REQUIRED_FAMILIES)
    with db.transaction() as conn:
        conn.execute("UPDATE cycles SET finished_at = ?, ok = ?, summary = ? WHERE id = ?", (
            iso(datetime.now(UTC)), int(result.ok),
            json.dumps({"fetched": result.fetched, "kept": result.kept, "failed": result.failed}), cycle_id))
    log.info("cycle done: ok=%s fetched=%s kept=%s failed=%s", result.ok, result.fetched, result.kept, result.failed)
    return result


def freshness(db: Database, now: datetime) -> dict[str, Any]:
    """Last successful cycle; `verified` is False after 12 hours without one (« Non vérifié »)."""
    with db.connect() as conn:
        row = conn.execute("SELECT MAX(started_at) FROM cycles WHERE ok = 1").fetchone()
    last = datetime.fromisoformat(row[0]) if row and row[0] else None
    return {"last_ok_cycle": iso(last), "verified": bool(last and now - last <= FRESHNESS_LIMIT)}
