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


def test_dossier_has_only_public_whitelisted_data(client: TestClient, ctx: AppContext,
                                                  opened: tuple[str, str]) -> None:
    csrf, vault_token = opened
    client.put("/api/private/coverage/acrylonitrile", json={"stock_weeks": 37.29, "supplier_lead_time_days": 613},
               headers=unsafe_headers(csrf, vault_token))
    event_id = suez_event(ctx)
    backend = ReplayBatchBackend()
    assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, Allowance())
    (requests,) = backend.batches.values()
    params = next(p for _, p in requests if event_id in p["messages"][0]["content"])
    text = params["messages"][0]["content"]
    assert event_id in text and "<reference_data>" in text and "chokepoint_suez" in text
    assert "37.29" not in json.dumps(params) and "613" not in json.dumps(params)
    assert "tools" not in params and params["output_config"]["effort"] == "high"
    assert "Aucun instrument de prix disponible" in text


def test_batch_analysis_then_verdict(ctx: AppContext) -> None:
    event_id = suez_event(ctx)
    backend = ReplayBatchBackend()
    counts = assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, Allowance())
    assert counts["batch"] >= 1 and counts["standard"] == 0
    # Only Suez weighs here; the other analyzed events (Rhine low water…) come back without impact.
    backend.responses = analyses_by_custom_id(ctx, lambda eid: impact_for(eid, severity=2 if eid == event_id else 0))
    assert assess.collect(ctx.db, ctx.budget, backend) >= 1
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
    backend.responses = analyses_by_custom_id(ctx, lambda eid: impact_for(eid, severity=2 if eid == rhine else 0))
    assess.collect(ctx.db, ctx.budget, backend)
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
    backend.responses = analyses_by_custom_id(ctx, lambda eid: impact_for(eid, injection=True))
    assess.collect(ctx.db, ctx.budget, backend)
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
    assert counts["standard"] == 1 and len(std.requests) == len(analysis.PARTS)
    (_, v), = assess.refresh_verdicts(ctx.db, ctx.vault, NOW, verified=True, last_ok_cycle=None)
    assert v.verdict in ("pot", "ant") and v.pressure == 2
    # Analyzed once: the next cycle does not send it again while the event does not change.
    assert assess.run_analyses(ctx.db, ctx.budget, std, ReplayBatchBackend(), NOW, Allowance())["standard"] == 0
    # Rules-only allowance sends nothing.
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE events SET lineages = lineages + 1 WHERE id = ?", (event_id,))
    assert assess.run_analyses(ctx.db, ctx.budget, std, ReplayBatchBackend(), NOW,
                               Allowance(rules_only=True))["standard"] == 0


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


def test_analysis_waits_for_every_part(ctx: AppContext) -> None:
    event_id = suez_event(ctx)
    aid, ids = _new_analysis(ctx, event_id)
    assert set(ids) == set(analysis.PARTS)
    assert all(len(c) <= 64 and c.replace("_", "").replace("-", "").isalnum() for c in ids.values())
    parts = analysis.split(impact_for(event_id))
    with ctx.db.transaction() as conn:
        assert analysis.store_part(conn, ids["comprehension"], message(parts["comprehension"]), 0.01) is None
        assert analysis.store_part(conn, ids["impacts"], message(parts["impacts"]), 0.01) is None
        assert conn.execute("SELECT state FROM analyses WHERE id = ?", (aid,)).fetchone()[0] == "pending"
        assert analysis.store_part(conn, ids["hypotheses"], message(parts["hypotheses"]), 0.01) == "done"
        row = conn.execute("SELECT state, cost_usd, result FROM analyses WHERE id = ?", (aid,)).fetchone()
    assert row["cost_usd"] == 0.03 and json.loads(row["result"])["schema_version"] == "1.0"


def test_a_failed_or_mismatched_part_fails_the_analysis(ctx: AppContext) -> None:
    event_id = suez_event(ctx)
    parts = analysis.split(impact_for(event_id))
    _, ids = _new_analysis(ctx, event_id)
    with ctx.db.transaction() as conn:
        analysis.store_part(conn, ids["comprehension"], message(parts["comprehension"]), 0.0)
        analysis.store_part(conn, ids["impacts"], None, 0.0)  # expired or errored request
        assert analysis.store_part(conn, ids["hypotheses"], message(parts["hypotheses"]), 0.0) == "error"
    # A part answered in the shape of another part is refused by its schema.
    _, ids = _new_analysis(ctx, event_id)
    with ctx.db.transaction() as conn:
        analysis.store_part(conn, ids["comprehension"], message(parts["comprehension"]), 0.0)
        analysis.store_part(conn, ids["impacts"], message(parts["hypotheses"]), 0.0)
        assert analysis.store_part(conn, ids["hypotheses"], message(parts["hypotheses"]), 0.0) == "error"
    # Parts about another event: rejected by the business checks.
    _, ids = _new_analysis(ctx, event_id)
    other = {**parts["impacts"], "event_id": "EVT-20261018-9999"}
    with ctx.db.transaction() as conn:
        analysis.store_part(conn, ids["comprehension"], message(parts["comprehension"]), 0.0)
        analysis.store_part(conn, ids["impacts"], message(other), 0.0)
        assert analysis.store_part(conn, ids["hypotheses"], message(parts["hypotheses"]), 0.0) == "rejected"
