"""Ledger entries of the personal scope and their outcomes (docs/SPEC.md, section 7, « Ma matière » column).

Public content: the scope, the verdict code, the price pressure and the keyed digest of the inputs, nothing more.
X, the action window, the threshold, the exposure, signals, answers and every outcome drawn from the private series
are private fields, encrypted apart (CLAUDE.md, rule 12: never re-encrypted, never modified).

Ground truth of a personal advice, in this order: the user's answer, their private series (quotes and purchases),
their supplier signals, then the public prices. A signal validates only advices issued before it, never the one it
caused. Without any price and without an answer within 14 days, the outcome is « non résolu ».
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from goulot.ledger import chain, rules
from goulot.ledger.rules import PENDING, UNRESOLVED, Outcome, Series
from goulot.private import records
from goulot.private.personal import TENSION_SIGNALS, Personal
from goulot.vault.keys import Vault

ANSWER_DELAY = timedelta(days=14)
SeriesFor = Callable[[str], Series | None]


def record_verdict(conn: sqlite3.Connection, vault: Vault, material_id: str, p: Personal, *, inputs_digest: str,
                   previous_pressure: int | None, at: datetime | None = None) -> int:
    v = p.verdict
    seq = chain.append(conn, "verdict", {
        "scope": "personal", "verdict": v.verdict, "pressure": v.pressure, "inputs_digest": inputs_digest,
    }, material_id=material_id, at=at, vault=vault, private={
        "label": v.label, "x_weeks": v.x_weeks, "x_low": v.x_low, "x_high": v.x_high,
        "window_weeks": v.window_weeks, "window_assumed": v.window_assumed, "event_ids": v.event_ids,
        "threshold_pct": p.threshold_pct, "revision": p.revision, "signals": p.signals_used,
        "exposure_pct": max((r.exposure for r in p.retained), default=0.0) * 100,
    })
    if v.pressure != (previous_pressure or 0) and v.pressure != 0:
        chain.append(conn, "pressure", {"scope": "personal", "pressure": v.pressure, "of_verdict_seq": seq},
                     material_id=material_id, at=at, vault=vault, private={"threshold_pct": p.threshold_pct})
    return seq


def record_signal(conn: sqlite3.Connection, vault: Vault, material_id: str, signal: dict[str, Any],
                  at: datetime | None = None) -> int:
    """A declared supplier signal; a tension signal with no personal warning in the previous 8 weeks is also a
    private missed move."""
    seq = chain.append(conn, "signal", {"scope": "personal"}, material_id=material_id, at=at, vault=vault,
                       private={k: signal.get(k) for k in ("id", "type", "date", "effective_date", "site_id", "pct",
                                                            "old_lead_days", "new_lead_days")})
    if signal["type"] in TENSION_SIGNALS:
        day = date.fromisoformat(signal["date"])
        warned = any(e["verdict"] in ("ant", "pot") and day - rules.CALM_WINDOW <= date.fromisoformat(e["at"][:10])
                     <= day for e in chain.entries(conn, material_id=material_id, kinds=("verdict",),
                                                   scope="personal"))
        if not warned:
            chain.append(conn, "missed_move", {"scope": "personal"}, material_id=material_id, at=at, vault=vault,
                         private={"day": signal["date"], "basis": "signal", "signal_id": signal.get("id")})
    return seq


def record_feedback(conn: sqlite3.Connection, vault: Vault, material_id: str, of_seq: int, answer: str,
                    at: datetime | None = None) -> int:
    if answer not in ("tension", "none"):
        raise ValueError("answer")
    return chain.append(conn, "user_feedback", {"scope": "personal", "of_seq": of_seq}, material_id=material_id,
                        at=at, vault=vault, private={"answer": answer})


def private_series(conn: sqlite3.Connection, vault: Vault, material_id: str) -> Series | None:
    """Quotes and purchases of the material as a price series, in their most frequent currency and unit."""
    points = [r for kind in ("quote", "purchase") for r in records.read_all(conn, vault, kind, material_id)
              if r.get("unit_price")]
    if not points:
        return None
    unit = Counter((r.get("currency", ""), r.get("unit", "")) for r in points).most_common(1)[0][0]
    by_day: dict[date, float] = {}
    for r in points:
        if (r.get("currency", ""), r.get("unit", "")) == unit:
            by_day[date.fromisoformat(r["date"])] = float(r["unit_price"])
    return Series(sorted(by_day.items()), rank=1, frequency="daily", name="série privée", source="user")


def _answers(conn: sqlite3.Connection, vault: Vault, material_id: str | None) -> dict[int, str]:
    out = {}
    for e in chain.entries(conn, material_id=material_id, kinds=("user_feedback",), scope="personal"):
        out[int(e["of_seq"])] = chain.private_fields(conn, vault, e["seq"]).get("answer", "")
    return out


def _signals(conn: sqlite3.Connection, vault: Vault, material_id: str) -> list[dict[str, Any]]:
    return [s for s in records.read_all(conn, vault, "signal", material_id) if s["type"] in TENSION_SIGNALS]


def _resolved(conn: sqlite3.Connection) -> set[int]:
    return {int(e["of_seq"]) for e in chain.entries(conn, kinds=("outcome",), scope="personal")}


def judge(entry: dict[str, Any], private: dict[str, Any], public: Series | None, mine: Series | None,
          answer: str | None, signals: list[dict[str, Any]], today: date) -> tuple[Outcome, str]:
    """Outcome of one personal verdict, and what decided it."""
    issued = date.fromisoformat(entry["at"][:10])
    thr = float(private.get("threshold_pct") or 5.0)
    caused = set(private.get("event_ids", [])) | set(private.get("signals", []))
    if entry["verdict"] in ("ant", "pot"):
        window_end = issued + timedelta(weeks=max(private.get("x_weeks") or 4.0, 4.0) + 2)
    else:
        window_end = issued + rules.CALM_WINDOW
    tension = answer == "tension" or any(
        s["id"] not in caused and issued < date.fromisoformat(s["date"]) <= window_end for s in signals)
    if entry["verdict"] in ("ant", "pot"):
        def by(series: Series | None) -> Outcome:
            return rules.warning(issued, private.get("x_weeks"), series, thr, today)
        good, bad = "confirmed", "false_alarm"
    else:
        def by(series: Series | None) -> Outcome:
            return rules.calm(issued, series, thr, today)
        good, bad = "correct_calm", "missed_rise"
    if tension:
        return Outcome(good if good == "confirmed" else bad, detail="tension constatée"), (
            "ta réponse" if answer == "tension" else "signal fournisseur")
    own = by(mine) if mine is not None else Outcome(UNRESOLVED)
    if own.result not in (PENDING, UNRESOLVED):
        return own, "série privée"
    market = by(public)
    if market.result == (good if good == "confirmed" else bad):
        return market, "prix public"
    if today <= window_end:
        return Outcome(PENDING), ""
    if answer == "none":
        return Outcome(bad if good == "confirmed" else good, detail="rien constaté"), "ta réponse"
    if market.result not in (PENDING, UNRESOLVED):
        return market, "prix public"
    if today <= window_end + ANSWER_DELAY:
        return Outcome(PENDING), ""  # the app asks the user, who has 14 days to answer
    return Outcome(UNRESOLVED, detail="ni prix ni réponse"), ""


def resolve(conn: sqlite3.Connection, vault: Vault, today: date, series_for: SeriesFor,
            at: datetime | None = None) -> int:
    done = _resolved(conn)
    answers = _answers(conn, vault, None)
    written = 0
    for e in chain.entries(conn, kinds=("verdict", "pressure"), scope="personal"):
        if e["seq"] in done or e.get("verdict") == "unverified":
            continue
        private = chain.private_fields(conn, vault, e["seq"])
        public = series_for(e["material_id"]) if e["material_id"] else None
        mine = private_series(conn, vault, e["material_id"]) if e["material_id"] else None
        if e["kind"] == "verdict":
            outcome, basis = judge(e, private, public, mine, answers.get(e["seq"]),
                                   _signals(conn, vault, e["material_id"]), today)
        else:
            issued = date.fromisoformat(e["at"][:10])
            sign = 1 if e["pressure"] > 0 else -1
            outcome = rules.pressure(issued, sign, mine, 2.0, today) if mine else Outcome(UNRESOLVED)
            basis = "série privée"
            if outcome.result == UNRESOLVED:
                outcome, basis = rules.pressure(issued, sign, public, 2.0, today), "prix public"
        if outcome.result == PENDING:
            continue
        chain.append(conn, "outcome", {"of_seq": e["seq"], "of_kind": e["kind"], "scope": "personal"},
                     material_id=e["material_id"], at=at, vault=vault, private={
                         "result": outcome.result, "observed_pct": outcome.observed_pct,
                         "on_index": outcome.on_index and basis == "prix public", "lead_weeks": outcome.lead_weeks,
                         "detail": outcome.detail, "basis": basis})
        written += 1
    return written


def questions(conn: sqlite3.Connection, vault: Vault, today: date, material_id: str | None = None
              ) -> list[dict[str, Any]]:
    """Matured personal advices without an outcome nor an answer: « As-tu constaté une tension ? »."""
    done = _resolved(conn)
    answers = _answers(conn, vault, material_id)
    out = []
    for e in chain.entries(conn, material_id=material_id, kinds=("verdict",), scope="personal"):
        if e["seq"] in done or e["seq"] in answers or e["verdict"] == "unverified":
            continue
        private = chain.private_fields(conn, vault, e["seq"])
        issued = date.fromisoformat(e["at"][:10])
        weeks = max(private.get("x_weeks") or 4.0, 4.0) + 2 if e["verdict"] in ("ant", "pot") else 8
        if today > issued + timedelta(weeks=weeks):
            out.append({"seq": e["seq"], "material_id": e["material_id"], "at": e["at"],
                        "label": private.get("label", ""), "window_end": (issued + timedelta(weeks=weeks)).isoformat()})
    return out


def entries(conn: sqlite3.Connection, vault: Vault, material_id: str | None = None) -> list[dict[str, Any]]:
    """Personal entries with their private fields, for the « Ma matière » column (vault open)."""
    return [{**e, **chain.private_fields(conn, vault, e["seq"])}
            for e in chain.entries(conn, material_id=material_id, scope="personal")]


def indicators(conn: sqlite3.Connection, vault: Vault, today: date, material_id: str | None = None
               ) -> dict[str, Any]:
    all_entries = entries(conn, vault, material_id)
    advices = {e["seq"]: e for e in all_entries if e["kind"] in ("verdict", "pressure")}
    outcomes = [o for o in all_entries if o["kind"] == "outcome" and o["of_seq"] in advices]
    first = min((date.fromisoformat(e["at"][:10]) for e in advices.values()), default=None)
    resolved = [o for o in outcomes if o["result"] != UNRESOLVED]

    def count(kind: str, good: set[str], verdicts: set[str] | None = None) -> dict[str, int]:
        rows = [o for o in outcomes if o["of_kind"] == kind and (
            verdicts is None or advices[o["of_seq"]].get("verdict") in verdicts)]
        done = [o for o in rows if o["result"] != UNRESOLVED]
        return {"good": sum(o["result"] in good for o in done), "resolved": len(done), "matured": len(rows),
                "on_index": sum(bool(o.get("on_index")) for o in done)}

    leads = [o["lead_weeks"] for o in outcomes if o.get("lead_weeks") is not None]
    return {
        "break_in": first is None or (today - first) < timedelta(days=90) or len(resolved) < 30,
        "since": first.isoformat() if first else None,
        "warnings_confirmed": count("verdict", {"confirmed"}, {"ant", "pot"}),
        "false_alarms": sum(o["result"] == "false_alarm" for o in outcomes),
        "calm_right": count("verdict", {"correct_calm"}, {"ras"}),
        "missed_rises": sum(o["result"] == "missed_rise" for o in outcomes)
        + sum(e["kind"] == "missed_move" for e in all_entries),
        "pressure_right": count("pressure", {"right_direction"}),
        "average_lead_weeks": round(sum(leads) / len(leads), 1) if leads else None,
        "resolved": len(resolved),
        "by_answer": sum(o.get("basis") == "ta réponse" for o in resolved),
        "by_private_series": sum(o.get("basis") == "série privée" for o in resolved),
    }
