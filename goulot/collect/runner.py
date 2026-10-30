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
from goulot.collect import hotspots, sources
from goulot.collect.base import Item, enabled, fold, iso, load_sources
from goulot.collect.rules import Rule, active_rules, match, material_names, too_old
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
MEDIA = "media"  # whole media feeds: their headlines also go to the hot spots
MAX_ITEMS_PER_SOURCE = 1_000  # bounded queue: a flooding feed cannot fill the database nor the triage
BACKOFF_BASE = timedelta(hours=3)
BACKOFF_MAX = timedelta(hours=24)
SITES_PER_QUERY = 8
NAMES_PER_GDELT_QUERY = 12
MISSING_KEY = "missing key"  # error of a source whose key is not in the keychain

SecretGetter = Callable[[str], str | None]


@dataclass
class CycleResult:
    cycle_id: str
    ok: bool
    fetched: int = 0
    kept: int = 0
    failed: list[str] = field(default_factory=list)


def _q(term: str) -> str:
    """A term as a GDELT or Google News phrase. Parentheses and quotes inside a name would break the query syntax
    (GDELT accepts parentheses only around OR'd terms): they become spaces."""
    term = " ".join(term.translate(str.maketrans('"()', "   ")).split())
    return f'"{term}"' if " " in term or "-" in term else term


def _any(terms: list[str]) -> str:
    """GDELT accepts parentheses only around OR'd terms."""
    return _q(terms[0]) if len(terms) == 1 else "(" + " OR ".join(_q(t) for t in terms) + ")"


def _public_terms(conn: sqlite3.Connection) -> list[tuple[str, list[str], list[str]]]:
    """(material id, names, site names) of each validated material, from the stored public rules only."""
    out = []
    for m in store.list_materials(conn):
        if m["status"] != "validated":
            continue
        material = store.get_material(conn, m["id"]) or {}
        sites = [r["value"] for r in conn.execute(
            "SELECT value FROM watch_rules WHERE material_id = ? AND active = 1 AND kind = 'keyword' "
            "AND node_key IS NOT NULL AND node_key IN (SELECT node_key FROM chain_nodes WHERE material_id = ? "
            "AND valid_to IS NULL AND node_type != 'country')", (m["id"], m["id"]))]
        out.append((m["id"], material_names(material), sites))
    return out


def queries(conn: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    """(query, material ids) for the news sources: one per material on its names, and one per group of sites
    combined with disruption terms. Built from the stored public rules only."""
    out: list[tuple[str, list[str]]] = []
    for material_id, names, sites in _public_terms(conn):
        if names:
            out.append((_any(names), [material_id]))
        for i in range(0, len(sites), SITES_PER_QUERY):
            out.append((f"{_any(sites[i:i + SITES_PER_QUERY])} ({DISRUPTION_TERMS})", [material_id]))
    return out


def gdelt_queries(conn: sqlite3.Connection) -> list[str]:
    """The same terms pooled across materials, for GDELT and its rate limit: fewer, fuller queries. Which material
    an article concerns is decided by the rule filter, not by the query that found it."""
    names: dict[str, str] = {}
    sites: dict[str, str] = {}
    for _, n, st in _public_terms(conn):
        names.update((fold(t), t) for t in n)
        sites.update((fold(t), t) for t in st)
    pooled, located = list(names.values()), list(sites.values())
    out = [_any(pooled[i:i + NAMES_PER_GDELT_QUERY]) for i in range(0, len(pooled), NAMES_PER_GDELT_QUERY)]
    out += [f"{_any(located[i:i + SITES_PER_QUERY])} ({DISRUPTION_TERMS})"
            for i in range(0, len(located), SITES_PER_QUERY)]
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
    # Ma chaîne's queries go to GDELT alone, as before: never pooled with the public ones.
    gdelt_qs = gdelt_queries(conn) + [q for q, _ in private_queries or []]
    boxes = [(r["node_key"], r["lat"], r["lon"]) for r in conn.execute(
        "SELECT DISTINCT node_key, lat, lon FROM watch_rules WHERE active = 1 AND kind = 'zone'")]
    # Names of the materials as the American press writes them: the names in other languages (rules of 29 October
    # 2026) would push them out of the six terms of the query.
    terms = sorted({fold(t): t for _, names, _ in _public_terms(conn) for t in names}.values())

    def news_gdelt(c: httpx2.Client, since: datetime) -> list[Item]:
        return sources.gdelt_many(c, gdelt_qs, since, now)

    def news_google(c: httpx2.Client, since: datetime) -> list[Item]:
        return [it for q, _ in qs for lang, cc in (("en", "US"), ("fr", "FR"))
                for it in sources.google_news(c, f"{q} when:7d", lang, cc)]

    def fires(c: httpx2.Client, since: datetime) -> list[Item]:
        key = secret("nasa_firms")
        if not key:
            raise sources.SourceError(MISSING_KEY)
        return sources.firms(c, key, boxes)

    fetchers: dict[str, Callable[[httpx2.Client, datetime], list[Item]]] = {
        "gdelt_doc": news_gdelt,
        "google_news_rss": news_google,
        "gdacs": lambda c, s: sources.gdacs(c),
        "usgs_earthquakes": lambda c, s: sources.usgs_quakes(c),
        "nasa_firms": fires,
        "noaa_nhc": lambda c, s: sources.nhc(c),
        "imf_portwatch_chokepoints": lambda c, s: sources.portwatch(c, now),
        "pegelonline": lambda c, s: sources.pegel(c),
        "federal_register": lambda c, s: sources.federal_register(c, terms, s) if terms else [],
        "copernicus_ems": lambda c, s: sources.copernicus(c, now),
        "eurlex_oj_l": lambda c, s: sources.eurlex(c, s),
    }
    for source_id, src in load_sources().items():
        if src.get("family") == MEDIA:
            fetchers[source_id] = _media(source_id, str(src["url"]), str(src.get("lang") or ""))
    return fetchers


def _media(source_id: str, url: str, lang: str) -> Callable[[httpx2.Client, datetime], list[Item]]:
    def read(c: httpx2.Client, since: datetime) -> list[Item]:
        return sources.media_feed(c, source_id, url, lang, since)

    return read


def _backoff(conn: sqlite3.Connection, source: str, now: datetime) -> bool:
    """True while a failing source must wait: 3 h after one failure, then doubling, at most 24 h. A run that failed
    for lack of a key is not a failure of the source: once the key is in the keychain, it is tried at the next
    cycle."""
    runs = conn.execute("SELECT ok, error, started_at FROM collect_runs WHERE source = ? ORDER BY id DESC LIMIT 8",
                        (source,)).fetchall()
    failures = 0
    for r in runs:
        if r["ok"] or r["error"].endswith(MISSING_KEY):
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


def ensure_rules(conn: sqlite3.Connection) -> int:
    """Catch-up, free and without AI. Nodes recorded before positions existed (27 October 2026) are located; a
    validated material whose nodes were just located, or that has no active rule at all, gets its probable route
    straits and its rules generated again. Returns how many materials got new rules."""
    from goulot.chain.places import locate_chain

    unplaced = [r[0] for r in conn.execute(
        "SELECT DISTINCT material_id FROM chain_nodes WHERE valid_to IS NULL AND coord_source = ''")]
    for material_id in unplaced:
        locate_chain(conn, material_id)
    missing = [r[0] for r in conn.execute(
        "SELECT id FROM materials m WHERE status = 'validated' AND NOT EXISTS "
        "(SELECT 1 FROM watch_rules w WHERE w.material_id = m.id AND w.active = 1)")]
    validated = {r[0] for r in conn.execute("SELECT id FROM materials WHERE status = 'validated'")}
    todo = list(dict.fromkeys([*missing, *(m for m in unplaced if m in validated)]))
    for material_id in todo:
        store.refresh_rules(conn, material_id)
    if unplaced or todo:
        log.warning("chain catch-up: located=%s rules_regenerated=%s", len(unplaced), len(todo))
    return len(todo)


def ensure_labels(db: Database, http: httpx2.Client) -> int:
    """Catch-up of 29 October 2026: names in German, Spanish and Italian of the validated materials identified
    before, asked once to Wikidata, then their rules generated again. A material whose question failed is asked
    again at the next cycle. Returns how many materials got their labels."""
    from goulot.chain.identify import wikidata_labels

    if not enabled("wikidata"):
        return 0
    with db.connect() as conn:
        todo = [(r["id"], r["cas"]) for r in conn.execute(
            "SELECT id, cas FROM materials WHERE status = 'validated' AND labels IS NULL")]
    done = 0
    for material_id, cas in todo:
        labels = wikidata_labels(http, cas)
        if labels is None:
            continue
        with db.transaction() as conn:
            conn.execute("UPDATE materials SET labels = ? WHERE id = ?",
                         (json.dumps(labels, ensure_ascii=False), material_id))
            if labels:
                store.refresh_rules(conn, material_id)
        done += 1
    return done


def run_cycle(db: Database, http: httpx2.Client, secret: SecretGetter, now: datetime | None = None,
              private: PrivateWatch | None = None) -> CycleResult:
    """`private`: rules and queries of Ma chaîne, built in memory by the caller; never stored nor logged."""
    now = now or datetime.now(UTC)
    cycle_id = str(uuid.uuid4())
    try:
        ensure_labels(db, http)
    except (ValueError, sqlite3.Error):
        log.exception("labels catch-up failed")
    with db.transaction() as conn:
        conn.execute("INSERT INTO cycles (id, started_at) VALUES (?, ?)", (cycle_id, iso(now)))
        ensure_rules(conn)
        fetchers = _fetchers(conn, now, secret, private.queries if private else None)
        rules = active_rules(conn) + (private.rules if private else [])
        country_nodes = {r[0] for r in conn.execute(
            "SELECT node_key FROM chain_nodes WHERE valid_to IS NULL AND node_type = 'country'")}
        country_nodes |= private.needs_material if private else set()
    result = CycleResult(cycle_id, ok=False)
    families_ok: dict[str, bool] = {}
    fetched_items: dict[str, list[Item]] = {}  # answers the world hot spots need (GDACS, Copernicus)
    for source_id, fetch in fetchers.items():
        if not enabled(source_id):
            continue
        family = str(load_sources()[source_id].get("family", ""))
        with db.connect() as conn:
            if not _due(conn, source_id, now):
                if not _backoff(conn, source_id, now):
                    families_ok[family] = True  # answered less than 3 hours ago: the family is covered
                continue
            since = (_last_success(conn, source_id) or now - FIRST_LOOKBACK) - OVERLAP
        started, error, fetched, matched, kept = iso(now), "", 0, 0, 0
        try:
            try:
                items = fetch(http, since)[:MAX_ITEMS_PER_SOURCE]
            except sources.PartialFailure as exc:
                items, error = exc.items[:MAX_ITEMS_PER_SOURCE], f"SourceError: {exc}"
                log.warning("source failed midway: source=%s error=%s kept_items=%s", source_id, error, len(items))
            fetched = len(items)
            if (source_id in ("gdacs", "copernicus_ems") or family == MEDIA) and not error:
                fetched_items[source_id] = items
            with db.transaction() as conn:
                for item in items:
                    hits = [] if too_old(item, now) else match(item, rules, {k: k for k in country_nodes})
                    if hits:
                        matched += 1
                        kept += int(_store(conn, item, hits, now))
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
                "INSERT INTO collect_runs (cycle_id, source, family, started_at, finished_at, ok, error, fetched, "
                "matched, kept) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cycle_id, source_id, family, started, iso(datetime.now(UTC)), int(ok), error[:120], fetched, matched,
                 kept),
            )
    try:
        hotspots.refresh(db, http, now, fetched_items)
    except sqlite3.Error:
        log.exception("hotspots refresh failed")
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


def material_watch(conn: sqlite3.Connection, material: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Watch state of one material for the dashboard. Only a validated chain with active rules is watched
    (`queries()`); `last_cycle` is the last successful cycle since its validation."""
    if material["status"] != "validated":
        return {"state": "chain_to_validate", "last_cycle": None, "hours_ago": None}
    if conn.execute("SELECT 1 FROM watch_rules WHERE material_id = ? AND active = 1 LIMIT 1",
                    (material["id"],)).fetchone() is None:
        return {"state": "no_rules", "last_cycle": None, "hours_ago": None}
    row = conn.execute(
        "SELECT MAX(c.started_at) FROM cycles c, materials m WHERE m.id = ? AND c.ok = 1 "
        "AND c.finished_at IS NOT NULL AND (m.validated_at IS NULL OR c.started_at >= m.validated_at)",
        (material["id"],)).fetchone()
    last = datetime.fromisoformat(row[0]) if row and row[0] else None
    if last is None:
        return {"state": "pending", "last_cycle": None, "hours_ago": None}
    hours = max(0, int((now - last).total_seconds() // 3600))
    return {"state": "active" if now - last <= FRESHNESS_LIMIT else "late", "last_cycle": iso(last),
            "hours_ago": hours}
