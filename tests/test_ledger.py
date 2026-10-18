"""Phase 3, lot 4: outcome rules on the fixed cases of tests/fixtures/ledger/, resolution and indicators."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from goulot.api.context import AppContext
from goulot.ledger import chain, outcomes, rules
from goulot.ledger.rules import Series
from goulot.settings import REPO_ROOT

CASES: dict[str, Any] = json.loads((REPO_ROOT / "tests" / "fixtures" / "ledger" / "cases.json").read_text())
TODAY = date.fromisoformat(CASES["today"])


def series(name: str | None) -> Series | None:
    if name is None:
        return None
    s = CASES["series"][name]
    return Series([(date.fromisoformat(d), v) for d, v in s["points"]], s["rank"], s["frequency"], name)


@pytest.mark.parametrize("case", CASES["advices"], ids=lambda c: c["name"])
def test_outcome_rules_on_fixed_cases(case: dict[str, Any]) -> None:
    issued = date.fromisoformat(case["issued"])
    s = series(case["series"])
    if case["kind"] == "verdict":
        out = (rules.calm(issued, s, 5.0, TODAY) if case["verdict"] == "ras"
               else rules.warning(issued, case["x_weeks"], s, 5.0, TODAY))
    elif case["kind"] == "hypothesis":
        out = rules.hypothesis(issued, case["horizon"], case["p10"], case["p90"], case["direction"], s, 2.0,
                               today=TODAY)
    else:
        out = rules.pressure(issued, case["pressure"], s, 2.0, TODAY)
    assert out.result == case["expected"]
    assert out.on_index == case.get("on_index", False)


def test_silence_is_never_a_success() -> None:
    issued = date(2026, 2, 9)
    assert rules.calm(issued, None, 5.0, issued + timedelta(weeks=9)).result == "unresolved"
    assert rules.calm(issued, None, 5.0, issued + timedelta(weeks=4)).result == "pending"
    gap = Series([(date(2026, 2, 2), 100.0), (date(2026, 6, 1), 101.0)], 3, "customs")
    assert rules.calm(issued, gap, 5.0, issued + timedelta(weeks=9)).result == "unresolved"  # nothing in the window
    assert rules.hypothesis(issued, "20d", 0, 5, "up", None, 2.0).result == "unresolved"
    assert rules.pressure(issued, 1, None, 2.0, issued + timedelta(weeks=20)).result == "unresolved"


def test_lead_time_and_missed_moves() -> None:
    rise = series("rise")
    assert rise is not None
    out = rules.warning(date.fromisoformat(CASES["advices"][0]["issued"]), 3, rise, 5.0, TODAY)
    assert out.lead_weeks is not None and 0 < out.lead_weeks <= 6
    missed = rules.missed_moves(rise, alerts=[], threshold_pct=5.0)
    assert len(missed) == 1  # one episode, not one per day
    warned = rules.missed_moves(rise, alerts=[missed[0] - timedelta(weeks=3)], threshold_pct=5.0)
    assert warned == []


def test_resolution_written_to_the_ledger(ctx: AppContext) -> None:
    rise, flat = series("rise"), series("flat")
    issued = datetime.fromisoformat(CASES["advices"][0]["issued"] + "T12:00:00+00:00")
    with ctx.db.transaction() as conn:
        a = chain.append(conn, "verdict", {"scope": "market", "verdict": "ant", "x_weeks": 3}, material_id="m1",
                         at=issued)
        b = chain.append(conn, "verdict", {"scope": "market", "verdict": "ras", "x_weeks": None}, material_id="m2",
                         at=issued)
        c = chain.append(conn, "verdict", {"scope": "market", "verdict": "pot", "x_weeks": 3}, material_id="m3",
                         at=issued)
        written = outcomes.resolve(conn, TODAY, {"m1": rise, "m2": flat, "m3": None}.get)
        assert written == 3
        assert outcomes.resolve(conn, TODAY, {"m1": rise, "m2": flat, "m3": None}.get) == 0  # once only
        results = {o["of_seq"]: o["result"] for o in chain.entries(conn, kinds=("outcome",))}
        stats = outcomes.indicators(conn, TODAY)
        assert chain.verify(conn).ok
    assert results == {a: "confirmed", b: "correct_calm", c: "unresolved"}
    assert stats["warnings_confirmed"] == {"good": 1, "resolved": 1, "matured": 2, "on_index": 0}
    assert stats["break_in"] is True  # fewer than 30 resolved advices: « pas encore assez de recul »


def test_thresholds_in_force_on_the_day_of_the_advice(ctx: AppContext) -> None:
    rise = series("rise")
    early = datetime(2026, 2, 6, 12, tzinfo=datetime.now().astimezone().tzinfo)
    with ctx.db.transaction() as conn:
        seq = chain.append(conn, "verdict", {"scope": "market", "verdict": "ant", "x_weeks": 3}, material_id="m",
                           at=early)
        # Raising the threshold afterwards does not rewrite history.
        chain.append(conn, "settings_change", {"values": {"threshold_pct": 50}}, at=early + timedelta(days=1))
        outcomes.resolve(conn, TODAY, lambda _: rise)
        (o,) = [o for o in chain.entries(conn, kinds=("outcome",)) if o["of_seq"] == seq]
        assert o["result"] == "confirmed"
        assert outcomes.thresholds_at(conn, (early + timedelta(days=2)).isoformat())["threshold_pct"] == 50
