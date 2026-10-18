"""Phase 3, lot 3: verdict, delay X, action window and price pressure (pure code, docs/SPEC.md, section 5)."""

from __future__ import annotations

import copy
import json
from typing import Any

from goulot.pipeline.verdict import ANTICIPATE, POTENTIAL, RAS, UNVERIFIED, EventInput, Settings, compute
from goulot.settings import REPO_ROOT

EXAMPLE: dict[str, Any] = json.loads(
    (REPO_ROOT / "schemas" / "examples" / "impact_assessment_v1.example.json").read_text(encoding="utf-8"))
COMMODITIES = {"copper"}
INSTRUMENTS = {"LME_CU_3M"}
# Node delays to the purchased material, in days (central, low, high, buffers on the path).
DELAYS = {"mine": {"days": 21.0, "low": 14.0, "high": 28.0, "buffers": 14.0},
          "far": {"days": 70.0, "low": 60.0, "high": 80.0, "buffers": 28.0}}


def analysis(severity: int = 2, effect: str = "tightening", duration_high: float | None = 30.0,
             hypotheses: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    a = copy.deepcopy(EXAMPLE)
    ci = a["commodity_impacts"][0]
    ci["severity"], ci["net_balance_effect"] = severity, effect
    ci["expected_duration_days"]["high"] = duration_high
    a["hypotheses"] = hypotheses or []
    return a


def hyp(p: float, p10: float, p90: float, direction: str = "up", unit: str = "percent") -> dict[str, Any]:
    return {"hypothesis_id": "H1", "instrument_id": "LME_CU_3M", "metric": "outright_price", "direction": direction,
            "horizon": "20d", "probability": p, "move_p10": p10, "move_p90": p90, "move_unit": unit,
            "rationale": "", "invalidation": ""}


def run(*events: EventInput, **kw: Any) -> Any:
    return compute(list(events), commodities=COMMODITIES, instruments=INSTRUMENTS, delays=DELAYS, **kw)


def test_nothing_to_report_and_freshness() -> None:
    assert run().verdict == RAS and run().pressure == 0
    assert run(verified=False).verdict == UNVERIFIED
    stale = run(EventInput("e", True, 0.9, ["mine"], analysis(severity=3)), verified=False)
    assert stale.verdict == ANTICIPATE  # only « Rien à signaler » turns into « Non vérifié »


def test_physical_rule_and_delay_x() -> None:
    v = run(EventInput("e", True, 0.9, ["mine"], analysis(severity=2)))
    assert v.verdict == ANTICIPATE and v.event_ids == ["e"]  # X = 3 weeks, W = 3 − 4 ≤ 2
    assert (v.x_weeks, v.x_low, v.x_high) == (3.0, 2.0, 4.0) and v.window_weeks == -1.0 and v.window_assumed
    far = run(EventInput("e", True, 0.9, ["far"], analysis(severity=2)))
    assert far.verdict == POTENTIAL and far.x_weeks == 10.0 and far.label == "Problème potentiel à 10 semaines"
    known = run(EventInput("e", True, 0.9, ["far"], analysis(severity=2)),
                settings=Settings(supplier_lead_weeks=8.5, supplier_lead_assumed=False))
    assert known.verdict == ANTICIPATE and not known.window_assumed  # W = 10 − 8.5 ≤ 2


def test_absorbed_event_keeps_only_price_effect() -> None:
    short = analysis(severity=3, duration_high=5)  # 5 days < 14 days of buffers on the path
    assert run(EventInput("e", True, 0.9, ["mine"], short)).verdict == RAS
    with_price = analysis(severity=3, duration_high=5, hypotheses=[hyp(0.6, 4, 12)])
    v = run(EventInput("e", True, 0.9, ["mine"], with_price))
    assert v.verdict == ANTICIPATE and v.x_weeks == 4.0  # price rule only: 20 trading days


def test_early_signals_need_a_reliable_lineage() -> None:
    weak = EventInput("e", False, 0.5, ["far"], analysis(severity=3))
    assert run(weak).verdict == RAS
    backed = EventInput("e", False, 0.7, ["far"], analysis(severity=3))
    assert run(backed).verdict == POTENTIAL
    assert run(EventInput("e", False, 0.7, ["mine"], analysis(severity=3))).verdict == POTENTIAL  # never « Anticiper »


def test_price_rules_and_units() -> None:
    assert run(EventInput("e", True, 0.9, ["far"], analysis(0, hypotheses=[hyp(0.55, 4, 12)]))).verdict == ANTICIPATE
    assert run(EventInput("e", True, 0.9, ["far"], analysis(0, hypotheses=[hyp(0.3, 2, 10)]))).verdict == POTENTIAL
    assert run(EventInput("e", True, 0.9, ["far"], analysis(0, hypotheses=[hyp(0.9, 0, 4)]))).verdict == RAS  # 2 % < 5 %
    units = analysis(0, hypotheses=[hyp(0.6, 400, 800, unit="price_units")])
    assert run(EventInput("e", True, 0.9, ["far"], units), last_prices={"LME_CU_3M": 9860}).verdict == ANTICIPATE
    assert run(EventInput("e", True, 0.9, ["far"], units)).verdict == RAS  # no last price: no conversion


def test_price_pressure() -> None:
    def pressure(a: dict[str, Any], confirmed: bool = True) -> int:
        return int(run(EventInput("e", confirmed, 0.9 if confirmed else 0.3, ["far"], a)).pressure)

    assert pressure(analysis(3)) == 2 and pressure(analysis(2)) == 1 and pressure(analysis(1)) == 0
    assert pressure(analysis(0, hypotheses=[hyp(0.3, 3, 9)])) == 1
    assert pressure(analysis(3, effect="loosening")) == -2 and pressure(analysis(2, effect="loosening")) == -1
    assert pressure(analysis(0, hypotheses=[hyp(0.3, -9, -3, direction="down")])) == -1
    assert pressure(analysis(3), confirmed=False) == 0  # an unbacked early signal moves nothing
    down = run(EventInput("e", True, 0.9, ["far"], analysis(2, effect="loosening")))
    assert down.verdict == RAS and "Pas d'urgence à acheter" in down.explanation


def test_rules_only_mode() -> None:
    v = run(EventInput("e", True, 0.9, ["mine"], None, exposed_share_pct=3.0), rules_only=True)
    assert v.verdict == ANTICIPATE and v.rules_only and "sans IA" in v.explanation and v.pressure == 2
    priced = analysis(0, hypotheses=[hyp(0.9, 10, 20)])
    assert run(EventInput("e", True, 0.9, ["far"], priced, exposed_share_pct=0.05), rules_only=True).verdict == RAS
