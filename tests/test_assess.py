"""Phase 3, lot 3: analyses (dossier, Batch or standard, checks, quarantine) and stored verdicts."""

from __future__ import annotations

import copy
import json
from typing import Any

from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.llm.backend import ReplayBackend
from goulot.llm.batch import ReplayBatchBackend
from goulot.pipeline import analysis, assess, grouping
from goulot.pipeline.assess import Allowance
from tests.conftest import unsafe_headers
from tests.test_collect import NOW, validated_chain
from tests.test_triage_events import collect_cycle
from tests.test_verdict import EXAMPLE


def impact_for(event_id: str, severity: int = 2, injection: bool = False) -> dict[str, Any]:
    a = copy.deepcopy(EXAMPLE)
    a["event_id"] = event_id
    a["injection_suspected"] = injection
    a["hypotheses"] = []
    understanding = a["event_understanding"]
    for item in understanding["facts"] + understanding["contradictions"]:
        item["source_ids"] = ["S1"]
    for e in understanding["affected_entities"]:
        e["reference_id"] = "R1"
    ci = a["commodity_impacts"][0]
    ci["commodity"], ci["severity"] = "acrylonitrile", severity
    ci["supply_at_risk"]["reference_ids"] = ["R1"]
    ci["expected_duration_days"].update({"low": 20, "high": 60})
    return a


def message(result: dict[str, Any]) -> dict[str, Any]:
    return {"id": "msg_a", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": "", "signature": "x"},
                                                   {"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "usage": {"input_tokens": 9_000, "output_tokens": 3_000}}


def suez_event(ctx: AppContext) -> str:
    validated_chain(ctx)
    collect_cycle(ctx)
    with ctx.db.transaction() as conn:
        grouping.events_from_measurements(conn)
        return str(conn.execute("SELECT id FROM events WHERE title LIKE '%Suez%'").fetchone()[0])


def analyses_by_custom_id(ctx: AppContext, make: Any) -> dict[str, dict[str, Any]]:
    """One answer per partial call, cut from the whole assessment that `make` returns for the event."""
    with ctx.db.connect() as conn:
        return {r["custom_id"]: message(analysis.split(make(r["event_id"]))[r["part"]]) for r in conn.execute(
            "SELECT p.custom_id, p.part, a.event_id FROM analysis_parts p JOIN analyses a ON a.id = p.analysis_id")}


def collect_all(ctx: AppContext, backend: ReplayBatchBackend, make: Any, now: Any = NOW) -> int:
    """Both steps of the pending Batch analyses: the impacts, then what they call for (sent by the next cycle)."""
    backend.responses = analyses_by_custom_id(ctx, make)
    done = assess.collect(ctx.db, ctx.budget, backend)
    assess.run_analyses(ctx.db, ctx.budget, None, backend, now, Allowance(analyze=False))
    backend.responses = analyses_by_custom_id(ctx, make)
    return done + assess.collect(ctx.db, ctx.budget, backend)


def test_dossier_has_only_public_whitelisted_data(client: TestClient, ctx: AppContext,
                                                  opened: tuple[str, str]) -> None:
    csrf, vault_token = opened
    client.put("/api/private/coverage/acrylonitrile", json={"stock_weeks": 37.2917, "supplier_lead_time_days": 613},
               headers=unsafe_headers(csrf, vault_token))
    event_id = suez_event(ctx)
    backend = ReplayBatchBackend()
    assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, Allowance())
    (requests,) = backend.batches.values()
    params = next(p for _, p in requests if event_id in p["messages"][0]["content"])
    text = params["messages"][0]["content"]
    assert event_id in text and "<reference_data>" in text and "chokepoint_suez" in text
    assert "37.2917" not in json.dumps(params) and "613" not in json.dumps(params)
    assert "tools" not in params and params["output_config"]["effort"] == "high"
    assert "Aucun instrument de prix disponible" in text


def test_batch_analysis_then_verdict(ctx: AppContext) -> None:
    event_id = suez_event(ctx)
    backend = ReplayBatchBackend()
    counts = assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, Allowance())
    assert counts["batch"] >= 1 and counts["standard"] == 0
    # Only Suez weighs here; the other analyzed events (Rhine low water…) come back without impact.
    assert collect_all(ctx, backend, lambda eid: impact_for(eid, severity=2 if eid == event_id else 0)) >= 1
    with ctx.db.connect() as conn:
        state = conn.execute("SELECT state FROM analyses WHERE event_id = ?", (event_id,)).fetchone()[0]
    assert state == "done"
    changed = assess.refresh_verdicts(ctx.db, ctx.vault, NOW, verified=True, last_ok_cycle=NOW.isoformat())
    (_, v), = changed
    # Suez → Rotterdam 12 d + Rotterdam stock 21 d + Rhine 5 d + 5 d = 43 d ≈ 6 weeks; window 6.1 − 4 > 2.
    assert v.verdict == "pot" and v.x_weeks == 6.1 and v.event_ids == [event_id]
    assert assess.refresh_verdicts(ctx.db, ctx.vault, NOW, verified=True, last_ok_cycle=None) == []  # unchanged
    with ctx.db.connect() as conn:
        row = conn.execute("SELECT * FROM verdicts WHERE current = 1").fetchone()
    assert row["verdict"] == "pot" and row["break_in"] == 1 and row["inputs"] is None  # vault not configured here


def test_low_rhine_close_to_the_user_means_anticipate(ctx: AppContext) -> None:
    suez_event(ctx)
    with ctx.db.connect() as conn:
        rhine = conn.execute("SELECT id FROM events WHERE title LIKE 'Basses eaux%'").fetchone()[0]
    backend = ReplayBatchBackend()
    assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, Allowance())
    collect_all(ctx, backend, lambda eid: impact_for(eid, severity=2 if eid == rhine else 0))
    (_, v), = assess.refresh_verdicts(ctx.db, ctx.vault, NOW, verified=True, last_ok_cycle=None)
    assert v.verdict == "ant" and v.event_ids == [rhine] and v.x_weeks == 0.7  # barge leg of 5 days


def test_verdict_inputs_encrypted_with_vault(ctx: AppContext) -> None:
    from tests.conftest import setup_vault

    setup_vault(ctx)
    suez_event(ctx)
    assess.refresh_verdicts(ctx.db, ctx.vault, NOW, verified=True, last_ok_cycle=None)
    with ctx.db.connect() as conn:
        row = conn.execute("SELECT * FROM verdicts WHERE current = 1").fetchone()
    assert row["inputs"] is not None and len(row["inputs_digest"]) == 64
    assert b"EVT-" not in bytes(row["inputs"])  # ciphertext only


def test_quarantine_and_rejection(ctx: AppContext) -> None:
    event_id = suez_event(ctx)
    backend = ReplayBatchBackend()
    assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, Allowance())
    collect_all(ctx, backend, lambda eid: impact_for(eid, injection=True))
    with ctx.db.connect() as conn:
        row = conn.execute("SELECT state, quarantine_reason FROM analyses WHERE event_id = ?", (event_id,)).fetchone()
    assert row["state"] == "quarantined" and "piégé" in row["quarantine_reason"]
    (_, v), = assess.refresh_verdicts(ctx.db, ctx.vault, NOW, verified=True, last_ok_cycle=None)
    assert v.verdict == "ras"  # a quarantined analysis never changes the verdict
    # Unknown commodity: rejected by the business checks.
    bad = impact_for(event_id)
    bad["commodity_impacts"][0]["commodity"] = "unobtainium"
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE events SET lineages = lineages + 1 WHERE id = ?", (event_id,))  # a new state
        event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        aid = analysis.create(conn, event, analysis.build_dossier(conn, event, NOW), "batch")
        assert analysis.store_result(conn, aid, message(bad), 0.0) == "rejected"


def test_single_unofficial_source_high_severity_is_quarantined(ctx: AppContext) -> None:
    event_id = suez_event(ctx)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE events SET official_source = 0, lineages = 1 WHERE id = ?", (event_id,))
        event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        aid = analysis.create(conn, event, analysis.build_dossier(conn, event, NOW), "batch")
        assert analysis.store_result(conn, aid, message(impact_for(event_id, severity=3)), 0.0) == "quarantined"


def test_urgent_event_goes_to_standard_api(ctx: AppContext) -> None:
    event_id = suez_event(ctx)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE events SET severity_hint = 3 WHERE id = ?", (event_id,))
    std = ReplayBackend()

    class Answering(ReplayBackend):
        def create(self, key: str, params: dict[str, Any]) -> dict[str, Any]:
            self.requests.append((key, params))
            part = next(p for p in analysis.PARTS if key.endswith("_" + p[:4]))
            return {**message(analysis.split(impact_for(event_id, severity=3))[part]), "_request_id": "req_x"}

    std = Answering()
    counts = assess.run_analyses(ctx.db, ctx.budget, std, ReplayBatchBackend(), NOW, Allowance())
    # The impacts, then the comprehension they call for; no hypotheses call without a price instrument.
    assert counts["standard"] == 1 and [k[-5:] for k, _ in std.requests] == ["_impa", "_comp"]
    (_, v), = assess.refresh_verdicts(ctx.db, ctx.vault, NOW, verified=True, last_ok_cycle=None)
    assert v.verdict in ("pot", "ant") and v.pressure == 2
    # Analyzed once: the next cycle does not send it again while the event does not change.
    assert assess.run_analyses(ctx.db, ctx.budget, std, ReplayBatchBackend(), NOW, Allowance())["standard"] == 0
    # Rules-only allowance sends nothing.
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE events SET lineages = lineages + 1 WHERE id = ?", (event_id,))
    assert assess.run_analyses(ctx.db, ctx.budget, std, ReplayBatchBackend(), NOW,
                               Allowance(rules_only=True))["standard"] == 0


def test_alarming_but_unconfirmed_event_waits_for_the_batch(ctx: AppContext) -> None:
    """Decision of 10 November 2026: the standard API, at full price, only for a severity hint of 3 on a confirmed or
    official fact; an unconfirmed alarming article is analysed in the Batch."""
    event_id = suez_event(ctx)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE events SET severity_hint = 3, confirmation = 'early_signal', official_source = 0 "
                     "WHERE id = ?", (event_id,))
        event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        assert not analysis.urgent(event) and analysis.should_analyze(conn, event)
    std, batched = ReplayBackend(), ReplayBatchBackend()
    counts = assess.run_analyses(ctx.db, ctx.budget, std, batched, NOW, Allowance())
    assert counts["standard"] == 0 and counts["batch"] >= 1
    assert any(event_id in cid for requests in batched.batches.values() for cid, _ in requests)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE events SET official_source = 1 WHERE id = ?", (event_id,))
        assert analysis.urgent(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())


def test_parts_are_merged_and_checked_by_the_code(ctx: AppContext) -> None:
    event_id = suez_event(ctx)
    whole = impact_for(event_id, severity=2)
    parts = analysis.split(whole)
    assert analysis.merge(parts) == whole  # merge is the inverse of split
    # A conclusion that contradicts the severities is aligned on them (the verdict reads the severities).
    parts["comprehension"]["assessment_status"] = "no_material_impact"
    assert analysis.merge(parts)["assessment_status"] == "impact_assessed"
    # One part flagged as booby-trapped flags the whole assessment.
    parts["hypotheses"]["injection_suspected"] = True
    assert analysis.merge(parts)["injection_suspected"] is True
    # Parts about different events never merge into a valid assessment.
    parts["impacts"]["event_id"] = "EVT-other"
    assert analysis.merge(parts)["event_id"] == ""


def _new_analysis(ctx: AppContext, event_id: str) -> tuple[str, dict[str, str]]:
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE events SET lineages = lineages + 1 WHERE id = ?", (event_id,))
        event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        aid = analysis.create(conn, event, analysis.build_dossier(conn, event, NOW), "batch")
        return aid, analysis.part_ids(conn, aid)


def test_analysis_in_two_steps(ctx: AppContext) -> None:
    """Decision of 10 November 2026: the impacts first; the comprehension only when an impact is found, the price
    hypotheses only when an instrument exists too."""
    event_id = suez_event(ctx)
    aid, ids = _new_analysis(ctx, event_id)
    assert set(ids) == {"impacts"}
    assert all(len(c) <= 64 and c.replace("_", "").replace("-", "").isalnum() for c in ids.values())
    parts = analysis.split(impact_for(event_id))
    with ctx.db.transaction() as conn:
        assert analysis.store_part(conn, ids["impacts"], message(parts["impacts"]), 0.01) is None
        assert conn.execute("SELECT state, batch_id FROM analyses WHERE id = ?", (aid,)).fetchone()[:] == ("pending",
                                                                                                         None)
        second = analysis.part_ids(conn, aid)
        assert set(second) == {"comprehension"}  # no price instrument in this dossier: no hypotheses call
        assert [(a, part) for a, _, part in analysis.unsent(conn)] == [(aid, "comprehension")]
        assert event_id in analysis.dossier_text(conn, aid)
        assert analysis.store_part(conn, second["comprehension"], message(parts["comprehension"]), 0.01) == "done"
        row = conn.execute("SELECT state, cost_usd, result, dossier_text FROM analyses WHERE id = ?", (aid,)).fetchone()
    result = json.loads(row["result"])
    assert row["cost_usd"] == 0.02 and result["schema_version"] == "1.0" and row["dossier_text"] is None
    assert result["hypotheses"] == [] and result["headline"] == parts["comprehension"]["headline"]


def test_no_impact_ends_the_analysis_at_the_first_step(ctx: AppContext) -> None:
    event_id = suez_event(ctx)
    aid, ids = _new_analysis(ctx, event_id)
    parts = analysis.split(impact_for(event_id, severity=0))
    with ctx.db.transaction() as conn:
        assert analysis.store_part(conn, ids["impacts"], message(parts["impacts"]), 0.01) == "done"
        assert analysis.unsent(conn) == [] and analysis.part_ids(conn, aid) == {}
        result = json.loads(conn.execute("SELECT result FROM analyses WHERE id = ?", (aid,)).fetchone()[0])
    assert result["assessment_status"] == "no_material_impact" and result["headline"] == analysis.NO_IMPACT_HEADLINE
    assert result["confidence"]["main_uncertainties"] == [analysis.SHORT_ANALYSIS]
    assert result["commodity_impacts"] == parts["impacts"]["commodity_impacts"]


def test_second_parts_follow_the_impacts_and_the_instruments() -> None:
    impacts = {"commodity_impacts": [{"severity": 0}, {"severity": 1}]}
    assert analysis.second_parts(impacts, {"instruments": ["CZCE_MA"]}) == ["comprehension", "hypotheses"]
    assert analysis.second_parts(impacts, {"instruments": []}) == ["comprehension"]
    assert analysis.second_parts({"commodity_impacts": [{"severity": 0}]}, {"instruments": ["CZCE_MA"]}) == []
    assert analysis.second_parts({"commodity_impacts": []}, {"instruments": ["CZCE_MA"]}) == []


def test_second_step_is_sent_by_the_next_cycle_even_in_economy_mode(ctx: AppContext) -> None:
    event_id = suez_event(ctx)
    backend = ReplayBatchBackend()
    assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, Allowance())
    (first,) = backend.batches.values()
    assert all(cid.endswith("_impa") for cid, _ in first)  # one call per event at first
    backend.responses = analyses_by_custom_id(ctx, lambda eid: impact_for(eid, severity=2 if eid == event_id else 0))
    assess.collect(ctx.db, ctx.budget, backend)
    with ctx.db.connect() as conn:
        states = dict(conn.execute("SELECT event_id, state FROM analyses").fetchall())
    assert states[event_id] == "pending" and all(s == "done" for e, s in states.items() if e != event_id)
    counts = assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, Allowance(analyze=False))
    assert counts["batch"] == 0 and len(backend.batches) == 2  # finished, not counted as a new analysis
    second = list(backend.batches.values())[1]
    assert [cid[-5:] for cid, _ in second] == ["_comp"]
    assert second[0][1]["messages"] == next(p for c, p in first if event_id in c)["messages"]  # the same dossier
    backend.responses = analyses_by_custom_id(ctx, lambda eid: impact_for(eid, severity=2))
    assert assess.collect(ctx.db, ctx.budget, backend) == 1
    assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, Allowance(analyze=False))
    assert len(backend.batches) == 2  # nothing is sent twice


def test_a_failed_or_mismatched_part_fails_the_analysis(ctx: AppContext) -> None:
    event_id = suez_event(ctx)
    parts = analysis.split(impact_for(event_id))
    _, ids = _new_analysis(ctx, event_id)
    with ctx.db.transaction() as conn:
        assert analysis.store_part(conn, ids["impacts"], None, 0.0) == "error"  # expired or errored request
    # A part answered in the shape of another part is refused by its schema.
    _, ids = _new_analysis(ctx, event_id)
    with ctx.db.transaction() as conn:
        assert analysis.store_part(conn, ids["impacts"], message(parts["hypotheses"]), 0.0) == "error"
    # The second step fails: the whole analysis fails, the impacts alone are not kept.
    aid, ids = _new_analysis(ctx, event_id)
    with ctx.db.transaction() as conn:
        analysis.store_part(conn, ids["impacts"], message(parts["impacts"]), 0.0)
        assert analysis.store_part(conn, analysis.part_ids(conn, aid)["comprehension"], None, 0.0) == "error"
    # Parts about another event: rejected by the business checks.
    aid, ids = _new_analysis(ctx, event_id)
    other = {**parts["comprehension"], "event_id": "EVT-20261018-9999"}
    with ctx.db.transaction() as conn:
        analysis.store_part(conn, ids["impacts"], message(parts["impacts"]), 0.0)
        assert analysis.store_part(conn, analysis.part_ids(conn, aid)["comprehension"], message(other),
                                   0.0) == "rejected"


def _analysed(ctx: AppContext, event_id: str, severity: int) -> None:
    backend = ReplayBatchBackend()
    assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, Allowance())
    collect_all(ctx, backend, lambda eid: impact_for(eid, severity=severity))


def _due(ctx: AppContext, event_id: str, at: Any) -> bool:
    with ctx.db.connect() as conn:
        return event_id in [e["id"] for e in assess.due_events(conn, Allowance(), now=at)]


def test_low_impact_event_is_analysed_again_only_when_it_gets_worse(ctx: AppContext) -> None:
    """Decision of 6 November 2026: once a day at most; after no impact or a severity of 1, only for a higher severity
    hint, a confirmation or an escalation — not for one more press echo."""
    from datetime import timedelta

    event_id = suez_event(ctx)
    _analysed(ctx, event_id, severity=0)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE events SET lineages = lineages + 1 WHERE id = ?", (event_id,))
    assert not _due(ctx, event_id, NOW + timedelta(days=2))  # one more echo: no new analysis
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE events SET severity_hint = severity_hint + 1 WHERE id = ?", (event_id,))
    assert not _due(ctx, event_id, NOW + timedelta(hours=3))  # worse, but analysed less than a day ago
    assert _due(ctx, event_id, NOW + timedelta(days=1, hours=1))


def test_serious_event_is_analysed_again_on_any_change(ctx: AppContext) -> None:
    from datetime import timedelta

    event_id = suez_event(ctx)
    _analysed(ctx, event_id, severity=2)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE events SET lineages = lineages + 1 WHERE id = ?", (event_id,))
    assert not _due(ctx, event_id, NOW + timedelta(hours=2))
    assert _due(ctx, event_id, NOW + timedelta(days=1, hours=1))


def test_small_hazard_never_analysed_whatever_the_path(ctx: AppContext) -> None:
    event_id = suez_event(ctx)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE events SET origin = 'hazard', severity_hint = 1, confirmation = 'confirmed' "
                     "WHERE id = ?", (event_id,))
        assert event_id not in [e["id"] for e in assess.due_events(conn, Allowance(), weighs={event_id}, now=NOW)]
        conn.execute("UPDATE events SET severity_hint = 2 WHERE id = ?", (event_id,))
        assert event_id in [e["id"] for e in assess.due_events(conn, Allowance(), weighs={event_id}, now=NOW)]
