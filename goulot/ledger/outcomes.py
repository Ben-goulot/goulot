"""Ledger writes and outcome resolution (docs/SPEC.md, section 7).

- Each change of market verdict or price pressure, and each price hypothesis of a valid analysis, becomes an
  entry the day it is issued.
- At each cycle, matured entries without an outcome get one (`outcome` entries pointing to `of_seq`); unresolved
  ones say so. Rises without a prior alert become `missed_move` entries.
- Indicators are computed from the ledger alone. During the break-in (90 days, or fewer than 30 resolved advices)
  the Fiabilité screen shows « pas encore assez de recul » instead of percentages.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from goulot.ledger import chain, rules
from goulot.ledger.rules import PENDING, Series

DEFAULT_THRESHOLD_PCT = 5.0
DEFAULT_MATERIALITY_PCT = 2.0
BREAK_IN_DAYS = 90
BREAK_IN_RESOLVED = 30
SeriesFor = Callable[[str], Series | None]


def record_verdict(conn: sqlite3.Connection, material_id: str, verdict: Any, *, inputs_digest: str,
                   versions: dict[str, Any], previous_pressure: int | None, at: datetime | None = None) -> int:
    """`verdict`: a `goulot.pipeline.verdict.Verdict` (market scope)."""
    seq = chain.append(conn, "verdict", {
        "scope": "market", "verdict": verdict.verdict, "label": verdict.label, "pressure": verdict.pressure,
        "x_weeks": verdict.x_weeks, "x_low": verdict.x_low, "x_high": verdict.x_high,
        "window_weeks": verdict.window_weeks, "rules_only": verdict.rules_only, "event_ids": verdict.event_ids,
        "inputs_digest": inputs_digest, "versions": versions,
    }, material_id=material_id, at=at)
    if verdict.pressure != (previous_pressure or 0) and verdict.pressure != 0:
        chain.append(conn, "pressure", {"scope": "market", "pressure": verdict.pressure, "of_verdict_seq": seq},
                     material_id=material_id, at=at)
    return seq


def record_hypotheses(conn: sqlite3.Connection, analysis_id: str, material_id: str, result: dict[str, Any],
                      versions: dict[str, Any], at: datetime | None = None) -> list[int]:
    seqs = []
    for h in result.get("hypotheses", []):
        seqs.append(chain.append(conn, "hypothesis", {
            "analysis_id": analysis_id, "event_id": result["event_id"], "hypothesis_id": h["hypothesis_id"],
            "instrument_id": h["instrument_id"], "metric": h["metric"], "direction": h["direction"],
            "horizon": h["horizon"], "probability": h["probability"], "move_p10": h["move_p10"],
            "move_p90": h["move_p90"], "move_unit": h["move_unit"], "versions": versions,
        }, material_id=material_id, at=at))
    return seqs


def thresholds_at(conn: sqlite3.Connection, day: str) -> dict[str, float]:
    """Thresholds in force on a given day: defaults, overridden by `settings_change` entries issued before."""
    values = {"threshold_pct": DEFAULT_THRESHOLD_PCT, "materiality_pct": DEFAULT_MATERIALITY_PCT}
    for e in chain.entries(conn, kinds=("settings_change",)):
        if e["at"] <= day:
            values.update({k: float(v) for k, v in e.get("values", {}).items() if k in values})
    return values


def _resolved(conn: sqlite3.Connection) -> set[int]:
    return {int(e["of_seq"]) for e in chain.entries(conn, kinds=("outcome",))}


def open_advice(conn: sqlite3.Connection, material_id: str) -> int:
    """Advices of a material still waiting for their outcome (« Non vérifié » is not an advice)."""
    done = _resolved(conn)
    return sum(1 for e in chain.entries(conn, material_id=material_id, kinds=("verdict", "hypothesis", "pressure"))
               if e["seq"] not in done and not e.get("superseded")
               and not (e["kind"] == "verdict" and e.get("verdict") not in ("ant", "pot", "ras")))


def resolve(conn: sqlite3.Connection, today: date, series_for: SeriesFor, at: datetime | None = None) -> int:
    """Append an outcome to every matured advice. Returns the number of outcomes written."""
    done = _resolved(conn)
    written = 0
    for e in chain.entries(conn, kinds=("verdict", "hypothesis", "pressure")):
        if e["seq"] in done or e.get("superseded"):
            continue
        issued = date.fromisoformat(e["at"][:10])
        limits = thresholds_at(conn, e["at"])
        series = series_for(e["material_id"]) if e["material_id"] else None
        if e["kind"] == "verdict":
            if e["verdict"] in ("ant", "pot"):
                outcome = rules.warning(issued, e["x_weeks"], series, limits["threshold_pct"], today)
            elif e["verdict"] == "ras":
                outcome = rules.calm(issued, series, limits["threshold_pct"], today)
            else:
                continue  # « Non vérifié » is not an advice
        elif e["kind"] == "hypothesis":
            outcome = rules.hypothesis(issued, e["horizon"], e["move_p10"], e["move_p90"], e["direction"], series,
                                       limits["materiality_pct"], e["move_unit"], today)
        else:
            outcome = rules.pressure(issued, 1 if e["pressure"] > 0 else -1, series, limits["materiality_pct"], today)
        if outcome.result == PENDING:
            continue
        chain.append(conn, "outcome", {
            "of_seq": e["seq"], "of_kind": e["kind"], "result": outcome.result, "observed_pct": outcome.observed_pct,
            "on_index": outcome.on_index, "lead_weeks": outcome.lead_weeks, "detail": outcome.detail,
            "series": series.name if series else "", "rank": series.rank if series else None,
        }, material_id=e["material_id"], at=at)
        written += 1
    return written


def record_missed_moves(conn: sqlite3.Connection, material_id: str, series: Series | None,
                        at: datetime | None = None) -> int:
    if series is None:
        return 0
    alerts = [date.fromisoformat(e["at"][:10]) for e in chain.entries(conn, material_id=material_id,
                                                                       kinds=("verdict",))
              if e["verdict"] in ("ant", "pot")]
    known = {e["day"] for e in chain.entries(conn, material_id=material_id, kinds=("missed_move",))}
    threshold = thresholds_at(conn, (at or datetime.now()).isoformat())["threshold_pct"]
    written = 0
    first_advice = min((date.fromisoformat(e["at"][:10]) for e in chain.entries(conn, material_id=material_id,
                                                                                 kinds=("verdict",))), default=None)
    for day in rules.missed_moves(series, alerts, threshold):
        if first_advice is None or day < first_advice or day.isoformat() in known:
            continue  # only rises that happened while Goulot was watching
        chain.append(conn, "missed_move", {"day": day.isoformat(), "series": series.name, "rank": series.rank},
                     material_id=material_id, at=at)
        written += 1
    return written


def indicators(conn: sqlite3.Connection, today: date, material_id: str | None = None) -> dict[str, Any]:
    """Market column of the Fiabilité screen, from the ledger only."""
    advices = {e["seq"]: e for e in chain.entries(conn, material_id=material_id,
                                                   kinds=("verdict", "hypothesis", "pressure"))}
    outcomes = [o for o in chain.entries(conn, material_id=material_id, kinds=("outcome",)) if o["of_seq"] in advices]
    first = min((date.fromisoformat(e["at"][:10]) for e in advices.values()), default=None)
    resolved = [o for o in outcomes if o["result"] != rules.UNRESOLVED]

    def count(kind: str, good: set[str], verdicts: set[str] | None = None) -> dict[str, int]:
        rows = [o for o in outcomes if o["of_kind"] == kind and (
            verdicts is None or advices[o["of_seq"]].get("verdict") in verdicts)]
        done = [o for o in rows if o["result"] != rules.UNRESOLVED]
        return {"good": sum(o["result"] in good for o in done), "resolved": len(done), "matured": len(rows),
                "on_index": sum(bool(o["on_index"]) for o in done)}

    warnings = count("verdict", {"confirmed"}, {"ant", "pot"})
    leads = [o["lead_weeks"] for o in outcomes if o.get("lead_weeks") is not None]
    break_in = first is None or (today - first) < timedelta(days=BREAK_IN_DAYS) or len(resolved) < BREAK_IN_RESOLVED
    return {
        "break_in": break_in,
        "since": first.isoformat() if first else None,
        "warnings_confirmed": warnings,
        "false_alarms": sum(o["result"] == "false_alarm" for o in outcomes),
        "calm_right": count("verdict", {"correct_calm"}, {"ras"}),
        "missed_rises": sum(o["result"] == "missed_rise" for o in outcomes)
        + len(chain.entries(conn, material_id=material_id, kinds=("missed_move",))),
        "pressure_right": count("pressure", {"right_direction"}),
        "ranges_hit": count("hypothesis", {"range_hit"}),
        "average_lead_weeks": round(sum(leads) / len(leads), 1) if leads else None,
        "resolved": len(resolved),
    }


def latest_head(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
    return str(row[0]) if row else chain.GENESIS


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
