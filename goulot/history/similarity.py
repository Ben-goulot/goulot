"""Similarity between a current event and the cases of the library (docs/SPEC.md, section 6).

Points: same event type (3), same node type (2), capacity share in the same band (2), same region (1), same season
(1), same market state, that is a margin widening or narrowing over 20 sessions on both dates (1). The five closest
verified cases at or above the threshold are kept. With fewer than 3, the screen says « pas assez de cas
comparables ». Always « hausse observée après », never « causée par ».
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from goulot.history import library, reactions
from goulot.ledger.rules import Series
from goulot.prices import margin

THRESHOLD = 5
KEEP = 5
MIN_CASES = 3
SHARE_BANDS = (1.0, 5.0, 15.0)  # %: below 1, 1 to 5, 5 to 15, 15 and more
REGIONS = {
    "east_asia": {"CN", "JP", "KR", "TW", "HK", "MN"},
    "south_east_asia": {"SG", "MY", "TH", "ID", "VN", "PH", "MM", "KH", "LA", "BN"},
    "south_asia": {"IN", "PK", "BD", "LK", "NP"},
    "middle_east": {"SA", "AE", "QA", "KW", "OM", "BH", "IR", "IQ", "IL", "JO", "TR", "SY", "LB", "YE"},
    "europe": {"DE", "FR", "NL", "BE", "LU", "IT", "ES", "PT", "GB", "IE", "AT", "CH", "PL", "CZ", "SK", "HU", "RO",
               "BG", "GR", "SE", "NO", "FI", "DK", "EE", "LV", "LT", "SI", "HR", "RS", "UA", "BY", "RU"},
    "north_america": {"US", "CA", "MX"},
    "south_america": {"BR", "AR", "CL", "PE", "CO", "VE", "EC", "BO", "UY", "PY"},
    "africa": {"ZA", "EG", "MA", "DZ", "NG", "CD", "ZM", "ZW", "GA", "GN", "GH", "KE", "MZ", "TZ", "AO", "NA", "LY"},
    "oceania": {"AU", "NZ", "PG", "NC"},
}
REGION_OF = {iso2: region for region, members in REGIONS.items() for iso2 in members}


def band(share: float | None) -> int | None:
    if share is None:
        return None
    return sum(share >= b for b in SHARE_BANDS)


def season(day: date) -> int:
    return (day.month % 12) // 3  # 0 winter (Dec-Feb), 1 spring, 2 summer, 3 autumn


def margin_state(index: Series | None, day: date) -> str | None:
    if index is None:
        return None
    trend = margin.trend(Series([p for p in index.points if p[0] <= day], index.rank, index.frequency))
    return trend[0] if trend and trend[0] != "flat" else None


@dataclass
class Target:
    """What is compared: the current event, or any situation described the same way."""

    event_type: str
    node_type: str
    share_pct: float | None
    country_iso2: str
    day: date


def score(target: Target, case: dict[str, Any], index: Series | None) -> int:
    points = 3 * (case["event_type"] == target.event_type) + 2 * (case["node_type"] == target.node_type)
    b = band(target.share_pct)
    points += 2 * (b is not None and b == band(case["capacity_share_pct"]))
    region = REGION_OF.get(target.country_iso2, target.country_iso2)
    points += int(target.country_iso2 not in ("", "unknown") and REGION_OF.get(case["country_iso2"],
                                                                               case["country_iso2"]) == region)
    points += int(season(target.day) == season(case["day"]))
    now_state = margin_state(index, target.day)
    points += int(now_state is not None and now_state == margin_state(index, case["day"]))
    return int(points)


def target_for_event(conn: sqlite3.Connection, event: sqlite3.Row) -> Target:
    linked = conn.execute(
        "SELECT n.node_type, l.supply_share_pct FROM event_links l JOIN chain_nodes n ON n.material_id = l.material_id "
        "AND n.node_key = l.node_key AND n.valid_to IS NULL WHERE l.event_id = ? ORDER BY "
        "COALESCE(l.supply_share_pct, -1) DESC", (event["id"],)).fetchall()
    return Target(event["event_type"], linked[0]["node_type"] if linked else "unknown",
                  linked[0]["supply_share_pct"] if linked else None, event["country_iso2"] or "unknown",
                  date.fromisoformat(event["first_seen_at"][:10]))


@dataclass
class Analogue:
    case: dict[str, Any]
    points: int
    reactions: list[reactions.Reaction]


def compare(conn: sqlite3.Connection, material_id: str, target: Target) -> tuple[list[Analogue], list[Analogue]]:
    """(kept, shown but excluded): verified cases announced before the target day, at or above the threshold, the
    closest five kept; drafts and overlapping cases above the threshold are shown, never counted. Prices known
    at the target day only (replay)."""
    candidates = library.cases(conn, material_id, statuses=("draft", "verified"), before=target.day)
    overlap = reactions.overlapping({c["id"]: c["day"] for c in candidates})
    index = margin.for_material(conn, material_id)
    scored = sorted(((score(target, c, index), c) for c in candidates), key=lambda pc: (-pc[0], -c_ord(pc[1])))
    above = [(p, c) for p, c in scored if p >= THRESHOLD]
    kept = [(p, c) for p, c in above if c["status"] == "verified" and c["id"] not in overlap][:KEEP]
    kept_ids = {c["id"] for _, c in kept}
    shown = [(p, c) for p, c in above if c["id"] not in kept_ids and (c["status"] == "draft" or c["id"] in overlap)]
    measured = reactions.for_cases(conn, material_id, [c for _, c in kept + shown], as_of=target.day)
    return ([Analogue(c, p, measured.get(c["id"], [])) for p, c in kept],
            [Analogue(c, p, measured.get(c["id"], [])) for p, c in shown])


def analogues(conn: sqlite3.Connection, material_id: str, target: Target) -> list[Analogue]:
    """The verified cases kept for the dossier and the medians."""
    return compare(conn, material_id, target)[0]


def c_ord(case: dict[str, Any]) -> int:
    return int(case["day"].toordinal())


def dossier_entries(found: list[Analogue], instrument_ids: list[str]) -> list[dict[str, Any]]:
    """`<analogues>` of the analysis dossier: verified cases only, reactions on an instrument of the dossier."""
    out = []
    for i, a in enumerate(found, 1):
        r = next((r for r in a.reactions if r.instrument_id in instrument_ids), None)
        c = a.case
        out.append({
            "id": f"A{i}", "title": c["title"], "date": c["first_report_date"], "type": c["event_type"],
            "node_type": c["node_type"], "capacity_share_pct": c["capacity_share_pct"], "direction": c["direction"],
            "duration_days": c["duration_days"], "similarity_points": a.points,
            "instrument_id": r.instrument_id if r else None,
            "own_change_pct": {str(h): v for h, v in r.own.items()} if r else None,
        })
    return out


def summary_line(found: list[Analogue], horizon: int = 20) -> str:
    """« 4 cas comparables (2017, 2021…) : variation propre médiane de +X % à 20 séances, entre a et b % »."""
    if len(found) < MIN_CASES:
        return "Pas assez de cas comparables."
    values, years = [], []
    for a in found:
        r = next((r for r in a.reactions if r.frequency == "daily" and r.own.get(horizon) is not None), None)
        if r is not None:
            values.append(r.own[horizon])
            years.append(a.case["day"].year)
    clean = [v for v in values if v is not None]
    if len(clean) < MIN_CASES:
        return f"{len(found)} cas comparables, mais trop peu de prix mesurés pour une médiane."
    median = reactions.quantile(clean, 0.5)
    low, high = reactions.quantile(clean, 0.1), reactions.quantile(clean, 0.9)
    return (f"{len(found)} cas comparables ({', '.join(str(y) for y in sorted(years))}) : variation propre médiane "
            f"de {_fr(median)} % à {horizon} séances, entre {_fr(low)} et {_fr(high)} % (de 10 % à 90 % des cas ; "
            "observée après, pas causée par).")


def _fr(value: float) -> str:
    return f"{value:+.1f}".replace(".", ",")
