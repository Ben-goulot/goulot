"""Price series in SQLite, links to materials, best series and proxy index (docs/SPEC.md, section 6).

Order of preference: rank 1 quote, 2 official price, 3 customs unit price (only if the HS code is the material's
own and the month's volume reaches a minimum; otherwise a weak index), 4 family index, 5 proxy index of the direct
inputs (equal weights by default), 6 fallback (Yahoo, Stooq) only for a series missing from ranks 1 to 5.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from goulot.db.connection import utcnow
from goulot.ledger.rules import Series

MIN_CUSTOMS_VOLUME = 1.0  # quantity units of the month; adjustable in Settings
FORWARD_FILL_DAYS = 7
SILENT_AFTER = {"daily": timedelta(hours=24), "tenday": timedelta(days=15), "monthly": timedelta(days=75),
                "customs": timedelta(days=75)}


def ensure_instrument(conn: sqlite3.Connection, source: str, code: str, name: str, *, rank: int, frequency: str,
                      unit: str = "", currency: str = "", params: dict[str, Any] | None = None) -> str:
    instrument_id = f"{source}:{code}"
    conn.execute(
        "INSERT OR IGNORE INTO instruments (id, source, code, name, unit, currency, rank, frequency, params, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (instrument_id, source, code, name, unit, currency, rank, frequency, json.dumps(params or {}), utcnow()),
    )
    return instrument_id


def link(conn: sqlite3.Connection, material_id: str, instrument_id: str, role: str, *, proposed_by: str,
         validated: bool = False, node_key: str | None = None, weight: float = 1.0) -> None:
    conn.execute(
        "INSERT INTO instrument_links (material_id, instrument_id, role, node_key, weight, validated, proposed_by, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(material_id, instrument_id, role) DO UPDATE SET "
        "validated = MAX(validated, excluded.validated)",
        (material_id, instrument_id, role, node_key, weight, int(validated), proposed_by, utcnow()),
    )


def upsert(conn: sqlite3.Connection, instrument_id: str, points: list[tuple[Any, ...]]) -> int:
    rows = [(instrument_id, p[0].isoformat(), float(p[1]), float(p[2]) if len(p) > 2 else None) for p in points]
    conn.executemany(
        "INSERT INTO prices (instrument_id, day, value, volume) VALUES (?, ?, ?, ?) ON CONFLICT(instrument_id, day) "
        "DO UPDATE SET value = excluded.value, volume = excluded.volume", rows)
    if rows:
        conn.execute("UPDATE instruments SET last_point_at = (SELECT MAX(day) FROM prices WHERE instrument_id = ?), "
                     "last_fetch_at = ? WHERE id = ?", (instrument_id, utcnow(), instrument_id))
    return len(rows)


def series(conn: sqlite3.Connection, instrument_id: str, *, since: date | None = None) -> Series | None:
    inst = conn.execute("SELECT * FROM instruments WHERE id = ?", (instrument_id,)).fetchone()
    if inst is None:
        return None
    rows = conn.execute("SELECT day, value, volume FROM prices WHERE instrument_id = ? AND day >= ? ORDER BY day",
                        (instrument_id, (since or date(1900, 1, 1)).isoformat())).fetchall()
    rank = inst["rank"]
    if inst["frequency"] == "customs":
        thin = any(r["volume"] is not None and r["volume"] < MIN_CUSTOMS_VOLUME for r in rows[-6:])
        rank = 4 if thin else rank  # a thin customs series is only a weak index
    points = [(date.fromisoformat(r["day"]), float(r["value"])) for r in rows]
    return Series(points, rank, inst["frequency"], inst["name"]) if points else None


def links(conn: sqlite3.Connection, material_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT l.*, i.rank, i.name, i.frequency, i.unit FROM instrument_links l JOIN instruments i "
                        "ON i.id = l.instrument_id WHERE l.material_id = ? ORDER BY i.rank, l.validated DESC",
                        (material_id,)).fetchall()


def proxy_index(conn: sqlite3.Connection, material_id: str) -> Series | None:
    """Rank 5: the direct inputs' best series, rebased to 100 and averaged with their weights."""
    inputs = [r for r in links(conn, material_id) if r["role"] == "input"]
    parts = []
    for r in inputs:
        s = series(conn, r["instrument_id"])
        if s and s.points:
            parts.append((s, float(r["weight"])))
    if not parts:
        return None
    start = max(s.points[0][0] for s, _ in parts)
    days = sorted({d for s, _ in parts for d, _ in s.points if d >= start})
    out = []
    bases = [s.last_before(start + timedelta(days=1)) for s, _ in parts]
    for day in days:
        total = weight = 0.0
        for (s, w), base in zip(parts, bases, strict=True):
            last = s.last_before(day)
            if base is None or last is None or (day - last[0]).days > FORWARD_FILL_DAYS * (5 if s.frequency != "daily"
                                                                                             else 1):
                break
            total += w * 100 * last[1] / base[1]
            weight += w
        else:
            out.append((day, round(total / weight, 4)))
    return Series(out, 5, "daily", "indice des intrants") if out else None


def best_series(conn: sqlite3.Connection, material_id: str) -> Series | None:
    """The best-ranked series of the material, validated links first; the proxy index if nothing else."""
    candidates = []
    for r in links(conn, material_id):
        if r["role"] in ("direct", "official", "customs", "index"):
            s = series(conn, r["instrument_id"])
            if s:
                candidates.append((s.rank, -r["validated"], s))
    if candidates:
        candidates.sort(key=lambda c: (c[0], c[1]))
        return candidates[0][2]
    return proxy_index(conn, material_id)


def silent_instruments(conn: sqlite3.Connection, now_day: date) -> list[str]:
    """Series silent beyond their normal rhythm (24 h daily, 15 days ten-day, 75 days monthly)."""
    out = []
    for r in conn.execute("SELECT id, frequency, last_point_at FROM instruments WHERE last_point_at IS NOT NULL"):
        limit = SILENT_AFTER.get(r["frequency"], timedelta(days=75))
        lag = timedelta(days=3) if r["frequency"] == "daily" else timedelta(0)  # weekends and holidays
        if now_day - date.fromisoformat(r["last_point_at"]) > limit + lag:
            out.append(r["id"])
    return out


def market(conn: sqlite3.Connection, material_id: str, since: date | None
           ) -> tuple[list[dict[str, Any]], set[str], dict[str, float]]:
    """What the analysis and the verdict may use for a material: the `<market_context>` list (linked series and
    the proxy index), the instruments a hypothesis may target (not the market factors), and their last prices."""
    from goulot.prices.stats import market_context

    contexts: list[dict[str, Any]] = []
    targets: set[str] = set()
    last: dict[str, float] = {}
    for r in links(conn, material_id):
        s = series(conn, r["instrument_id"])
        if s is None:
            continue
        ctx = market_context(r["instrument_id"], s, r["unit"], since)
        ctx["role"] = r["role"]
        contexts.append(ctx)
        last[r["instrument_id"]] = s.points[-1][1]
        if r["role"] != "factor":
            targets.add(r["instrument_id"])
    proxy = proxy_index(conn, material_id)
    if proxy is not None:
        pid = f"proxy:{material_id[:8]}"
        contexts.append({**market_context(pid, proxy, "indice 100", since), "role": "proxy"})
        targets.add(pid)
        last[pid] = proxy.points[-1][1]
    return contexts, targets, last
