"""Price reactions to past cases, computed by the code, never by the AI (docs/SPEC.md, section 6).

For each series linked to the material: change at 1, 5, 20 and 60 sessions (1, 2 and 3 points for a monthly
series; 1, 2 and 6 for the ten-day official prices) after the last price known before the first public report.
Own change = raw change − β × change of the market factor over the same days: Brent for an organic material (its
formula holds carbon and hydrogen), none otherwise (lithium, silicon, metals, mineral salts), and β is estimated on
the 250 daily sessions before the report (120 at least). Two cases of the same material less than 10 days apart
overlap: both are left out of the medians by default. Computed from the stored prices at each request, so that
a new price or a corrected series is always taken into account; `as_of` only uses what was known at that date.
"""

from __future__ import annotations

import itertools
import re
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from goulot.ledger.rules import Series, pct
from goulot.prices import store as prices

DAILY_HORIZONS = (1, 5, 20, 60)
SLOW_HORIZONS = {"monthly": (1, 2, 3), "customs": (1, 2, 3), "tenday": (1, 2, 6)}
BETA_WINDOW = 250
BETA_MIN = 120
OVERLAP_DAYS = 10
BRENT = "eia:RBRTE"
ORGANIC = re.compile(r"C(?![a-z])")


def factor_for(formula: str) -> str | None:
    """Brent for an organic formula (carbon and hydrogen), none otherwise."""
    return BRENT if ORGANIC.search(formula or "") and re.search(r"H(?![a-z])", formula or "") else None


def horizons(frequency: str) -> tuple[int, ...]:
    return DAILY_HORIZONS if frequency == "daily" else SLOW_HORIZONS.get(frequency, (1, 2, 3))


def beta(series: Series, factor: Series, base_day: date) -> float | None:
    """OLS slope of the series' daily returns on the factor's, over the sessions before `base_day`."""
    days = [d for d in series.dates if d <= base_day][-(BETA_WINDOW + 1):]
    values = dict(series.points)
    factor_values = dict(factor.points)
    pairs = []
    for a, b in itertools.pairwise(days):
        if a in factor_values and b in factor_values and values[a] and factor_values[a]:
            pairs.append((values[b] / values[a] - 1, factor_values[b] / factor_values[a] - 1))
    if len(pairs) < BETA_MIN:
        return None
    mx = sum(x for _, x in pairs) / len(pairs)
    my = sum(y for y, _ in pairs) / len(pairs)
    var = sum((x - mx) ** 2 for _, x in pairs)
    return sum((x - mx) * (y - my) for y, x in pairs) / var if var else None


@dataclass
class Reaction:
    instrument_id: str
    name: str
    frequency: str
    base_day: date
    raw: dict[int, float | None]
    own: dict[int, float | None]
    beta: float | None
    factor: str | None
    notes: list[str] = field(default_factory=list)


def reaction(series: Series, instrument_id: str, day: date, factor: Series | None, factor_id: str | None,
             as_of: date | None = None) -> Reaction | None:
    """The reaction of one series to a case first reported on `day`; None without a price before."""
    base = series.last_before(day - timedelta(days=1))
    if base is None or (day - base[0]).days > (10 if series.frequency == "daily" else 100):
        return None
    b = beta(series, factor, base[0]) if factor is not None and series.frequency == "daily" else None
    notes = []
    if factor_id and factor is None:
        notes.append("Facteur de marché sans série : variation brute seulement.")
    elif factor_id and b is None:
        notes.append("Bêta non estimable (moins de 120 séances) : variation brute seulement.")
    raw: dict[int, float | None] = {}
    own: dict[int, float | None] = {}
    for h in horizons(series.frequency):
        point = series.nth_after(base[0], h)
        if point is None or (as_of is not None and point[0] > as_of):
            raw[h] = own[h] = None
            continue
        change = round(pct(base[1], point[1]), 2)
        raw[h] = own[h] = change
        if b is not None and factor is not None:
            f0, f1 = factor.last_before(base[0]), factor.last_before(point[0])
            if f0 is not None and f1 is not None:
                own[h] = round(change - b * pct(f0[1], f1[1]), 2)
    return Reaction(instrument_id, series.name, series.frequency, base[0], raw, own,
                    round(b, 3) if b is not None else None, factor_id if b is not None else None, notes)


def overlapping(days: dict[int, date]) -> set[int]:
    """Case rows reported less than 10 days from another case of the same material."""
    rows = sorted(days, key=lambda r: days[r])
    out: set[int] = set()
    for a, b in itertools.pairwise(rows):
        if (days[b] - days[a]).days < OVERLAP_DAYS:
            out |= {a, b}
    return out


def for_cases(conn: sqlite3.Connection, material_id: str, cases: list[dict[str, Any]],
              as_of: date | None = None) -> dict[int, list[Reaction]]:
    """Reactions of every linked series (factors excepted) to each case."""
    material = conn.execute("SELECT formula FROM materials WHERE id = ?", (material_id,)).fetchone()
    factor_id = factor_for(material["formula"] if material else "")
    factor = prices.series(conn, factor_id) if factor_id else None
    links = [r for r in prices.links(conn, material_id) if r["role"] != "factor"]
    loaded = {r["instrument_id"]: prices.series(conn, r["instrument_id"]) for r in links}
    out: dict[int, list[Reaction]] = {}
    for c in cases:
        found = []
        for iid, s in loaded.items():
            if s is None:
                continue
            r = reaction(s, iid, c["day"], factor, factor_id, as_of)
            if r is not None and any(v is not None for v in r.raw.values()):
                found.append(r)
        out[c["id"]] = found
    return out


def quantile(values: list[float], q: float) -> float:
    """Linear interpolation between the closest ranks (inclusive method)."""
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


@dataclass
class Summary:
    instrument_id: str
    horizon: int
    n: int
    median: float | None
    low: float | None  # 10th percentile
    high: float | None  # 90th percentile
    years: list[int]
    excluded_overlap: int


def summarize(cases: list[dict[str, Any]], reactions: dict[int, list[Reaction]], instrument_id: str, horizon: int,
              *, include_overlap: bool = False) -> Summary:
    """Median own change of the verified cases on one series and horizon, with its 10 % to 90 % band. Drafts never
    count."""
    verified = [c for c in cases if c["status"] == "verified"]
    overlap = overlapping({c["id"]: c["day"] for c in cases if c["status"] != "rejected"})
    values, years, excluded = [], [], 0
    for c in verified:
        if c["id"] in overlap and not include_overlap:
            excluded += 1
            continue
        r = next((r for r in reactions.get(c["id"], []) if r.instrument_id == instrument_id), None)
        value = r.own.get(horizon) if r is not None else None
        if value is not None:
            values.append(value)
            years.append(c["day"].year)
    return Summary(instrument_id, horizon, len(values), round(statistics.median(values), 2) if values else None,
                   round(quantile(values, 0.1), 2) if values else None,
                   round(quantile(values, 0.9), 2) if values else None, sorted(years), excluded)
