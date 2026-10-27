"""Daily price collection and the links proposed when a chain is validated (docs/SPEC.md, section 6).

At validation: the customs unit price of each HS code of the material (rank 3), the official Chinese price of the
material and of its direct inputs when the NBS publishes one (rank 2), the Shanghai or Zhengzhou quote of the
material or of a direct input (rank 1), all to validate by the user, and the market factors (Brent, WTI, Henry
Hub) are proposed. The user adds or validates the others from the material sheet. The first fetch downloads all
the history the source keeps (three years for the exchanges' daily files, since 2016 for the NBS releases); the
next ones complete it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2

from goulot.chain import store as chain_store
from goulot.collect.base import enabled
from goulot.db.connection import Database
from goulot.prices import czce, exchange_files, nbs, shfe, sources
from goulot.prices import store as prices
from goulot.prices.sources import PriceSourceError

log = logging.getLogger("goulot.prices")

FACTORS = {"RBRTE": ("Brent (EIA)", "USD/bbl"), "RWTC": ("WTI (EIA)", "USD/bbl"),
           "RNGWHHD": ("Gaz naturel Henry Hub (EIA)", "USD/MMBtu")}
EXCHANGES = (shfe.EXCHANGE, czce.EXCHANGE)
SHARED_SOURCES = {nbs.SOURCE, *(ex.source for ex in EXCHANGES)}  # one download serves all their series
HISTORY_START = {"daily": date(1987, 1, 1), "monthly": date(1990, 1, 1), "customs": date(2013, 1, 1),
                 "tenday": date(2010, 1, 1)}
REFRESH_OVERLAP = timedelta(days=45)


def propose_links(conn: sqlite3.Connection, material_id: str) -> None:
    """Inside the validation transaction."""
    material = chain_store.get_material(conn, material_id)
    if material is None:
        return
    for hs in material.get("hs_codes", []):
        code = str(hs["code"])
        name = f"Prix unitaire à l'import (États-Unis), SH {code}"
        iid = prices.ensure_instrument(conn, "us_census_trade", code, name, rank=3, frequency="customs",
                                       unit="USD/unité", currency="USD", params={"hs6": code})
        prices.link(conn, material_id, iid, "customs", proposed_by="reconstruction")
    for ex in EXCHANGES:
        quoted = ex.by_cas(material["cas"])
        if quoted is not None:
            exchange_files.ensure_instruments(conn, ex)
            prices.link(conn, material_id, ex.instrument_id(quoted), "direct", proposed_by="reconstruction")
    official = nbs.BY_CAS.get(material["cas"])
    if official is not None:
        nbs.ensure_instruments(conn)
        prices.link(conn, material_id, nbs.instrument_id(official), "official", proposed_by="reconstruction")
    for node in direct_inputs(conn, material_id):
        for ex in EXCHANGES:  # a daily quote first (rank 1)
            quote = ex.match_node(node["node_key"], node["product"], node["name"])
            if quote is not None:
                exchange_files.ensure_instruments(conn, ex)
                prices.link(conn, material_id, ex.instrument_id(quote), "input", proposed_by="reconstruction",
                            node_key=node["node_key"])
        product = nbs.match_node(node["node_key"], node["product"], node["name"])
        if product is not None and product is not official:
            nbs.ensure_instruments(conn)
            prices.link(conn, material_id, nbs.instrument_id(product), "input", proposed_by="reconstruction",
                        node_key=node["node_key"])
    for code, (name, unit) in FACTORS.items():
        iid = prices.ensure_instrument(conn, "eia", code, name, rank=1, frequency="daily", unit=unit, currency="USD")
        prices.link(conn, material_id, iid, "factor", proposed_by="auto", validated=True)


def direct_inputs(conn: sqlite3.Connection, material_id: str) -> list[sqlite3.Row]:
    """Material nodes that reach the purchased material without going through another material node: an input of
    the purchased material itself (tier 1), or of the plants, countries or recyclers that make it (tier 2 or more,
    as in a chain « soda ash → refinery → lithium carbonate »). The graph is oriented towards the purchased one."""
    nodes = {r["node_key"]: r for r in conn.execute(
        "SELECT node_key, node_type, product, name, tier FROM chain_nodes WHERE material_id = ? AND valid_to IS NULL",
        (material_id,))}
    sinks = {k for k, r in nodes.items() if r["tier"] == 0}
    downstream: dict[str, set[str]] = {}
    for e in conn.execute("SELECT from_key, to_key FROM chain_edges WHERE material_id = ? AND valid_to IS NULL",
                          (material_id,)):
        downstream.setdefault(e["from_key"], set()).add(e["to_key"])

    def reaches_sink(start: str) -> bool:
        seen, todo = {start}, list(downstream.get(start, ()))
        while todo:
            key = todo.pop()
            if key in sinks:
                return True
            if key in seen or key not in nodes or nodes[key]["node_type"] == "material":
                continue  # another material in between: an input of an input
            seen.add(key)
            todo.extend(downstream.get(key, ()))
        return False

    return [r for k, r in sorted(nodes.items(), key=lambda kr: (kr[1]["tier"], kr[0]))
            if r["node_type"] == "material" and k not in sinks and reaches_sink(k)]


SecretGetter = Callable[[str], str | None]
PricePoint = tuple[Any, ...]
KEYED_APIS = {"eia", "us_census_trade", "fred", "bls_ppi"}


def _fetch(client: httpx2.Client, source: str, code: str, start: date, secret: SecretGetter) -> list[PricePoint]:
    def key(name: str) -> str:
        value = secret(name)
        if not value:
            raise PriceSourceError("missing key")
        return value

    if source in KEYED_APIS:
        sources.pause()  # one second before each call to a keyed API: never a burst (terms of service)
    if source == "eia":
        return list(sources.eia(client, key("eia"), code, start))
    if source == "fred":
        return list(sources.fred(client, key("fred"), code, start))
    if source == "bls_ppi":
        return list(sources.bls(client, secret("bls"), [code], start.year, date.today().year).get(code, []))
    if source == "us_census_trade":
        return list(sources.census_unit_prices(client, secret("census"), code, start))
    if source == "eurostat":
        dataset, _, filters = code.partition("|")
        return list(sources.eurostat(client, dataset, dict(f.split("=", 1) for f in filters.split("&") if f)))
    if source == "yahoo_chart":
        return list(sources.yahoo(client, code))
    if source == "stooq":
        return list(sources.stooq(client, code))
    raise PriceSourceError("source not wired")


def _due(last_fetch: str | None, frequency: str, now: datetime) -> bool:
    if last_fetch is None:
        return True
    gap = timedelta(hours=20) if frequency == "daily" else timedelta(days=7)
    return now - datetime.fromisoformat(last_fetch) >= gap


def catch_up_links(db: Database) -> int:
    """Materials validated before the price links existed (phase 2) get their proposals once. Returns how many."""
    with db.transaction() as conn:
        missing = [r[0] for r in conn.execute(
            "SELECT id FROM materials m WHERE status = 'validated' AND NOT EXISTS "
            "(SELECT 1 FROM instrument_links l WHERE l.material_id = m.id)")]
        for material_id in missing:
            propose_links(conn, material_id)
    return len(missing)


def watched_instruments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Instruments of the validated materials, and of the archived ones while one of their advices waits for an
    outcome (silence is never a success: without prices the advice could only end « non résolu »)."""
    from goulot.chain import lifecycle

    kept = [r[0] for r in conn.execute("SELECT id FROM materials WHERE status = 'archived'")
            if lifecycle.keeps_prices(conn, r[0])]
    return conn.execute(
        "SELECT DISTINCT i.* FROM instruments i JOIN instrument_links l ON l.instrument_id = i.id "
        "JOIN materials m ON m.id = l.material_id WHERE m.status = 'validated' "
        "OR m.id IN (SELECT value FROM json_each(?))", (json.dumps(kept),)).fetchall()


def run(db: Database, client: httpx2.Client, secret: SecretGetter, now: datetime | None = None) -> dict[str, int]:
    """Fetch every due series linked to a validated material. Returns points written per instrument."""
    now = now or datetime.now(UTC)
    catch_up_links(db)
    with db.connect() as conn:
        rows = watched_instruments(conn)
    written: dict[str, int] = {}
    if enabled(nbs.SOURCE) and any(r["source"] == nbs.SOURCE for r in rows) and any(
            r["source"] == nbs.SOURCE and _due(r["last_fetch_at"], "tenday", now) for r in rows):
        try:  # one list page a day, then the releases not read yet: every NBS series at once
            written[nbs.SOURCE] = nbs.collect(db, client)
        except (httpx2.HTTPError, ValueError) as exc:
            log.warning("price source failed: source=%s error=%s", nbs.SOURCE, type(exc).__name__)
        else:
            with db.transaction() as conn:
                conn.execute("UPDATE instruments SET last_fetch_at = ? WHERE source = ?",
                             (now.isoformat(timespec="seconds"), nbs.SOURCE))
    for ex in EXCHANGES:
        mine = [r for r in rows if r["source"] == ex.source]
        if not enabled(ex.source) or not mine:
            continue
        filling = exchange_files.days_read(db, ex) < exchange_files.HISTORY_DAYS * 5 // 7 - 30  # holidays aside
        if not filling and not any(_due(r["last_fetch_at"], "daily", now) for r in mine):
            continue
        try:  # one file per trading day for every product: at most 40 files a run while the history fills in
            written[ex.source] = exchange_files.collect(db, client, ex, now.date())
        except (httpx2.HTTPError, ValueError) as exc:
            log.warning("price source failed: source=%s error=%s", ex.source, type(exc).__name__)
            continue
        with db.transaction() as conn:
            conn.execute("UPDATE instruments SET last_fetch_at = ? WHERE source = ?",
                         (now.isoformat(timespec="seconds"), ex.source))
    for inst in rows:
        if inst["source"] in SHARED_SOURCES:
            continue
        if not enabled(inst["source"]) or not _due(inst["last_fetch_at"], inst["frequency"], now):
            continue
        start = (date.fromisoformat(inst["last_point_at"]) - REFRESH_OVERLAP if inst["last_point_at"]
                 else HISTORY_START.get(inst["frequency"], date(2000, 1, 1)))
        try:
            points = _fetch(client, inst["source"], inst["code"], start, secret)
        except (httpx2.HTTPError, PriceSourceError, ValueError, KeyError) as exc:
            log.warning("price source failed: instrument=%s error=%s", inst["id"], type(exc).__name__)
            continue
        with db.transaction() as conn:
            written[inst["id"]] = prices.upsert(conn, inst["id"], points)
            conn.execute("UPDATE instruments SET last_fetch_at = ? WHERE id = ?", (now.isoformat(timespec="seconds"),
                                                                                  inst["id"]))
    return written


def backfill(db: Database, client: httpx2.Client, secret: SecretGetter, material_id: str,
             progress: Callable[[str], None] = lambda line: None, now: datetime | None = None) -> dict[str, int]:
    """All the history of the material's linked series, now (`goulot history backfill`). Points written per series."""
    now = now or datetime.now(UTC)
    catch_up_links(db)
    with db.transaction() as conn:  # an explicit download proposes again what the chain allows (idempotent)
        before = {r["instrument_id"] for r in prices.links(conn, material_id)}
        propose_links(conn, material_id)
        added = [r for r in prices.links(conn, material_id) if r["instrument_id"] not in before]
    for r in added:
        progress(f"Série proposée, à valider sur la fiche : {r['name']}"
                 + (f" (intrant « {r['node_key']} »)" if r["node_key"] else ""))
    with db.connect() as conn:
        rows = conn.execute("SELECT DISTINCT i.* FROM instruments i JOIN instrument_links l ON l.instrument_id = i.id "
                            "WHERE l.material_id = ?", (material_id,)).fetchall()
    written: dict[str, int] = {}
    sources_seen = {r["source"] for r in rows}
    if nbs.SOURCE in sources_seen and enabled(nbs.SOURCE):
        progress("Prix officiels chinois (communiqués du Bureau national des statistiques)…")
        written[nbs.SOURCE] = nbs.collect(db, client)
    for ex in EXCHANGES:
        if ex.source not in sources_seen or not enabled(ex.source):
            continue
        progress(f"Bourse ({ex.source}) : un fichier par jour de cotation, trois ans…")
        total = 0
        while True:
            n_before = exchange_files.days_read(db, ex)
            total += exchange_files.collect(db, client, ex, now.date(), max_days=200)
            if exchange_files.days_read(db, ex) == n_before:
                break
        written[ex.source] = total
    for inst in rows:
        if inst["source"] in SHARED_SOURCES or not enabled(inst["source"]):
            continue
        progress(f"{inst['name']}…")
        try:
            points = _fetch(client, inst["source"], inst["code"], HISTORY_START.get(inst["frequency"],
                                                                                     date(2000, 1, 1)), secret)
        except PriceSourceError as exc:  # its messages are written by our code, keys masked
            progress(f"  indisponible : {'clé absente' if str(exc) == 'missing key' else exc}")
            continue
        except (httpx2.HTTPError, ValueError, KeyError) as exc:  # their text may hold an address: type only
            progress(f"  indisponible ({type(exc).__name__})")
            continue
        with db.transaction() as conn:
            written[inst["id"]] = prices.upsert(conn, inst["id"], points)
        if points:
            days = [p[0] for p in points]
            progress(f"  {len(points)} points, du {min(days)} au {max(days)}")
        else:
            progress("  aucune donnée renvoyée par la source")
    return written

