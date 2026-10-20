"""Margin of a quoted material over its inputs, as an index (docs/SPEC.md, section 6).

The chain type gives no yields and the series do not share a unit or a currency, so the margin is measured as an
index: the material's price rebased to 100, divided by the proxy index of its direct inputs (also rebased to 100).
A margin that widens over 20 sessions points to a tension of the material itself; prices rising together without
widening point to a cost push. Computed only for a material with a quote or an official price (rank 1 or 2) and
at least one priced input.
"""

from __future__ import annotations

import sqlite3

from goulot.ledger.rules import Series
from goulot.prices import store as prices

SESSIONS = 20
FLAT_PCT = 1.0  # below this change in 20 sessions, the margin is « stable »
MAX_GAP_DAYS = 7


def margin_index(material: Series, inputs: Series) -> Series | None:
    if not material.points or not inputs.points:
        return None
    start = max(material.points[0][0], inputs.points[0][0])
    base_m, base_i = material.last_before(start), inputs.last_before(start)  # on or before `start`
    if base_m is None or base_i is None:
        return None
    out = []
    for day, value in material.points:
        if day < start:
            continue
        last = inputs.last_before(day)
        if last is None or (day - last[0]).days > MAX_GAP_DAYS:
            continue
        out.append((day, round(100 * (value / base_m[1]) / (last[1] / base_i[1]), 4)))
    return Series(out, 5, material.frequency, "marge relative (indice)", "goulot") if len(out) > 1 else None


def trend(index: Series | None, sessions: int = SESSIONS) -> tuple[str, float] | None:
    """(« up » | « down » | « flat », change in % over the last `sessions` points), or None if too short."""
    if index is None or len(index.points) <= sessions:
        return None
    last, before = index.points[-1][1], index.points[-1 - sessions][1]
    change = 100 * (last / before - 1)
    return ("flat" if abs(change) < FLAT_PCT else "up" if change > 0 else "down"), round(change, 2)


def for_material(conn: sqlite3.Connection, material_id: str) -> Series | None:
    best = prices.best_series(conn, material_id)
    if best is None or best.rank > 2:
        return None
    inputs = prices.proxy_index(conn, material_id)
    return margin_index(best, inputs) if inputs is not None else None
