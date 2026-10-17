"""Quote before a reconstruction (CLAUDE.md, Budget): maximum searches × 0.01 $ plus estimated tokens.

The estimate is what the screen shows; the maximum is the worst case (all searches, long context, one retry of
the core structuring call, the context call) and is what the cap check uses. Token estimates come from the
phase 2 test runs.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from goulot.chain import research, structure
from goulot.llm.pricing import Usage, cost_usd, web_search_price

QUOTE_TTL = timedelta(minutes=15)

# Measured on 2026-10-16 (acrylonitrile, 20 searches): 548,037 tokens read, 13,097 written.
# Second run (direct search, 18 searches): 409,924 read, 11,463 written.
RESEARCH_TOKENS = {"expected": (450_000, 14_000), "max": (700_000, research.MAX_TOKENS)}
STRUCTURE_TOKENS = {"expected": (25_000, 16_000), "max": (40_000, structure.MAX_TOKENS)}
CONTEXT_TOKENS = {"expected": (20_000, 6_000), "max": (30_000, structure.CONTEXT_MAX_TOKENS)}


@dataclass(frozen=True)
class Quote:
    research_usd: float
    research_max_usd: float
    structure_usd: float
    structure_max_usd: float  # per attempt of the core call
    context_usd: float
    context_max_usd: float  # one attempt, optional call
    searches_max: int

    @property
    def estimate_usd(self) -> float:
        return round(self.research_usd + self.structure_usd + self.context_usd, 2)

    @property
    def max_usd(self) -> float:
        return round(self.research_max_usd + structure.MAX_ATTEMPTS * self.structure_max_usd + self.context_max_usd, 2)


def reconstruction_quote() -> Quote:
    searches = research.MAX_SEARCHES
    search_cost = searches * web_search_price()

    def tokens(model: str, pair: tuple[int, int]) -> float:
        return cost_usd(model, Usage(input_tokens=pair[0], output_tokens=pair[1]))

    return Quote(
        research_usd=round(tokens(research.MODEL, RESEARCH_TOKENS["expected"]) + search_cost, 4),
        research_max_usd=round(tokens(research.MODEL, RESEARCH_TOKENS["max"]) + search_cost, 4),
        structure_usd=round(tokens(structure.MODEL, STRUCTURE_TOKENS["expected"]), 4),
        structure_max_usd=round(tokens(structure.MODEL, STRUCTURE_TOKENS["max"]), 4),
        context_usd=round(tokens(structure.MODEL, CONTEXT_TOKENS["expected"]), 4),
        context_max_usd=round(tokens(structure.MODEL, CONTEXT_TOKENS["max"]), 4),
        searches_max=searches,
    )


def save_quote(conn: sqlite3.Connection, material_id: str, quote: Quote, now: datetime) -> str:
    quote_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO quotes (id, material_id, kind, estimate_usd, max_usd, detail, created_at, expires_at) "
        "VALUES (?, ?, 'reconstruction', ?, ?, ?, ?, ?)",
        (quote_id, material_id, quote.estimate_usd, quote.max_usd, json.dumps(asdict(quote)),
         now.isoformat(timespec="seconds"), (now + QUOTE_TTL).isoformat(timespec="seconds")),
    )
    return quote_id


def confirm_quote(conn: sqlite3.Connection, quote_id: str, material_id: str, now: datetime) -> float | None:
    """Marks the quote confirmed; returns its maximum, or None if unknown, expired or already used."""
    row = conn.execute(
        "SELECT max_usd, expires_at, confirmed_at FROM quotes WHERE id = ? AND material_id = ?", (quote_id, material_id)
    ).fetchone()
    if row is None or row["confirmed_at"] or datetime.fromisoformat(row["expires_at"]) < now.astimezone(UTC):
        return None
    conn.execute("UPDATE quotes SET confirmed_at = ? WHERE id = ?", (now.isoformat(timespec="seconds"), quote_id))
    return float(row["max_usd"])
