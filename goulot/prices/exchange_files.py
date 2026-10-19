"""Daily files of the Chinese exchanges (rank 1, docs/SPEC.md, section 6): one public file per trading day with
every contract. For each product of an exchange's allowlist, the series takes the settlement price of the contract
with the largest open interest (the « dominant » contract). When the dominant contract changes, the series may jump
by the spread between two maturities: the reactions computed from it use several sessions, and the jump is part of
the market noise they are compared with. Days already read, days without trading included, are kept in
`exchange_days` and never fetched again.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import httpx2

from goulot.collect.base import fold
from goulot.db.connection import Database, utcnow
from goulot.net.client import fetch
from goulot.prices import store as prices

log = logging.getLogger("goulot.prices.exchanges")

MAX_BYTES = 2 * 1024 * 1024
MAX_DAYS_PER_RUN = 40  # the history fills in over a few days of cycles, politely
HISTORY_DAYS = 3 * 365


class UnreadableFile(ValueError):
    pass


@dataclass(frozen=True)
class Product:
    code: str  # contract prefix (« cu », « MA »…)
    fr: str
    cas: str | None
    unit: str
    aliases: tuple[str, ...] = ()  # folded names of chain nodes that are this product


@dataclass(frozen=True)
class Exchange:
    source: str
    url: str  # formatted with {ymd} and {year}
    products: tuple[Product, ...]
    parse: Callable[[str], dict[str, float]]  # file text → {product code: dominant settlement price}

    def instrument_id(self, product: Product) -> str:
        return f"{self.source}:{product.code}"

    def by_cas(self, cas: str) -> Product | None:
        return next((p for p in self.products if p.cas == cas), None)  # the first product listed wins

    def match_node(self, node_key: str, product: str, name: str = "") -> Product | None:
        return next((p for p in self.products if mentions(p.aliases, node_key, product, name)), None)


def words(text: str) -> str:
    """Folded text reduced to words separated by single spaces (« Carbonate de soude (Na₂CO₃) » → « carbonate de
    soude na2co3 »)."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", fold(text)).split())


def mentions(aliases: tuple[str, ...], *texts: str) -> bool:
    """True if one of the aliases appears as whole words in the node id, product or name of a chain node. The
    proposal is only a suggestion: the user validates it on the material sheet."""
    padded = [f" {words(t)} " for t in texts if t]
    return any(f" {words(a)} " in t for a in aliases for t in padded)


def dominant(rows: list[tuple[str, float, float]]) -> dict[str, float]:
    """(product code, settlement, open interest) rows → settlement of the largest open interest per product."""
    best: dict[str, tuple[float, float]] = {}
    for code, settlement, interest in rows:
        if settlement > 0 and settlement == settlement and interest > best.get(code, (0.0, -1.0))[1]:
            best[code] = (settlement, interest)
    return {code: value for code, (value, _) in best.items()}


def ensure_instruments(conn: sqlite3.Connection, ex: Exchange) -> None:
    for p in ex.products:
        prices.ensure_instrument(conn, ex.source, p.code, f"{p.fr}, contrat le plus traité", rank=1,
                                 frequency="daily", unit=p.unit, currency="CNY")


def collect(db: Database, client: httpx2.Client, ex: Exchange, today: date, *, start: date | None = None,
            max_days: int = MAX_DAYS_PER_RUN) -> int:
    """Read the weekdays not read yet, newest first, back to `start`. Returns prices written."""
    start = start or today - timedelta(days=HISTORY_DAYS)
    with db.transaction() as conn:
        ensure_instruments(conn, ex)
        known = {r[0] for r in conn.execute("SELECT day FROM exchange_days WHERE source = ?", (ex.source,))}
    days: list[date] = []
    d = today
    while d >= start and len(days) < max_days:
        if d.weekday() < 5 and d.isoformat() not in known:
            days.append(d)
        d -= timedelta(days=1)
    written = 0
    for day in days:
        result = fetch(client, ex.url.format(ymd=day.strftime("%Y%m%d"), year=day.year), max_bytes=MAX_BYTES)
        if result.status not in (200, 404):
            raise httpx2.HTTPError(f"HTTP {result.status}")
        found: dict[str, float] = {}
        if result.status == 200:
            try:
                found = ex.parse(result.content.decode("utf-8", errors="replace"))
            except (UnreadableFile, ValueError):
                log.warning("exchange file unreadable: source=%s day=%s", ex.source, day)
                continue  # not marked: read again at the next run
        with db.transaction() as conn:
            for code, value in found.items():
                written += prices.upsert(conn, f"{ex.source}:{code}", [(day, value)])
            if day < today:  # today's file may not be published yet
                conn.execute("INSERT OR IGNORE INTO exchange_days (source, day, products, fetched_at) "
                             "VALUES (?, ?, ?, ?)", (ex.source, day.isoformat(), len(found), utcnow()))
    return written


def days_read(db: Database, ex: Exchange) -> int:
    with db.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM exchange_days WHERE source = ?", (ex.source,)).fetchone()[0])
