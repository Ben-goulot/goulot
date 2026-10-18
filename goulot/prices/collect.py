"""Daily price collection and the links proposed when a chain is validated (docs/SPEC.md, section 6).

At validation: the customs unit price of each HS code of the material (rank 3, to validate by the user) and
the market factors (Brent, WTI, Henry Hub) are proposed. The user adds or validates the others from the material
sheet. The first fetch downloads all the history the source keeps; the next ones complete it.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2

from goulot.chain import store as chain_store
from goulot.collect.base import enabled
from goulot.db.connection import Database
from goulot.prices import sources
from goulot.prices import store as prices
from goulot.prices.sources import PriceSourceError

log = logging.getLogger("goulot.prices")

FACTORS = {"RBRTE": ("Brent (EIA)", "USD/bbl"), "RWTC": ("WTI (EIA)", "USD/bbl"),
           "RNGWHHD": ("Gaz naturel Henry Hub (EIA)", "USD/MMBtu")}
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
    for code, (name, unit) in FACTORS.items():
        iid = prices.ensure_instrument(conn, "eia", code, name, rank=1, frequency="daily", unit=unit, currency="USD")
        prices.link(conn, material_id, iid, "factor", proposed_by="auto", validated=True)


SecretGetter = Callable[[str], str | None]
PricePoint = tuple[Any, ...]


def _fetch(client: httpx2.Client, source: str, code: str, start: date, secret: SecretGetter) -> list[PricePoint]:
    def key(name: str) -> str:
        value = secret(name)
        if not value:
            raise PriceSourceError("missing key")
        return value

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


def run(db: Database, client: httpx2.Client, secret: SecretGetter, now: datetime | None = None) -> dict[str, int]:
    """Fetch every due series linked to a validated material. Returns points written per instrument."""
    now = now or datetime.now(UTC)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT i.* FROM instruments i JOIN instrument_links l ON l.instrument_id = i.id "
            "JOIN materials m ON m.id = l.material_id WHERE m.status = 'validated'").fetchall()
    written: dict[str, int] = {}
    for inst in rows:
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
