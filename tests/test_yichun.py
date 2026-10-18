"""Functional case « Yichun 2025 » (docs/SPEC.md, section 13), on the recorded chain of lithium carbonate.

On 24 July 2025, permit problems are reported at eight lithium mines around Yichun. Replayed on that day, the
pipeline must bring the material to « Problème potentiel » at least. This tests the plumbing, not Goulot's skill:
the model already knows how the story ended, and the AI answers here are written for the test.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from typing import Any

import httpx2

from goulot.api.context import AppContext
from goulot.chain import service, store
from goulot.cli.dev import FIXTURES
from goulot.collect.runner import run_cycle
from goulot.llm.backend import ReplayBackend
from goulot.llm.batch import ReplayBatchBackend
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from goulot.pipeline import analysis, assess, grouping, triage
from goulot.pipeline.assess import Allowance
from tests.test_assess import message
from tests.test_triage_events import answers_for, result
from tests.test_verdict import EXAMPLE

DAY = datetime(2025, 7, 24, 12, tzinfo=UTC)
ARTICLES = [
    {"url": "https://www.reuters.com/markets/commodities/yichun-lithium-mines-permits-2025-07-24/",
     "title": "Lithium carbonate: Yichun lithium mines face permit review, output at risk",
     "seendate": "20250724T060000Z", "language": "English"},
    {"url": "https://www.mining.com/yichun-eight-lithium-mines-ordered-to-resubmit-reserve-reports/",
     "title": "Eight Yichun lepidolite mines told to redo reserve reports; lithium supply in question",
     "seendate": "20250724T090000Z", "language": "English"},
]


def recorded_lithium_chain(ctx: AppContext) -> str:
    directory = FIXTURES / "554-13-2"
    identity = json.loads((directory / "identity.json").read_text(encoding="utf-8"))
    with ctx.db.transaction() as conn:
        material_id = store.create_material(conn, identity)
    service.reconstruct(service.Deps(ctx.db, ctx.budget, ReplayBackend(directory), None), material_id,
                        service.start_job(ctx.db, material_id), budget_name="dev")
    with ctx.db.transaction() as conn:
        store.validate_material(conn, material_id, acknowledge_injection=False)
    return material_id


def gdelt(articles: list[dict[str, str]]) -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.gdeltproject.org":
            return httpx2.Response(200, json={"articles": articles})
        return httpx2.Response(404)

    return build_client(Allowlist(set()), transport=httpx2.MockTransport(handler))


def impact(event_id: str) -> dict[str, Any]:
    a = copy.deepcopy(EXAMPLE)
    a["event_id"], a["hypotheses"], a["injection_suspected"] = event_id, [], False
    understanding = a["event_understanding"]
    for item in understanding["facts"] + understanding["contradictions"]:
        item["source_ids"] = ["S1"]
    for e in understanding["affected_entities"]:
        e["reference_id"] = "R1"
    ci = a["commodity_impacts"][0]
    ci.update(commodity="lithium_carbonate", severity=2, net_balance_effect="tightening")
    ci["supply_at_risk"].update(reference_ids=["R1"], global_share_pct=None, basis="unknown")
    ci["expected_duration_days"].update(low=30, high=120)
    return a


def test_yichun_2025_replay(ctx: AppContext) -> None:
    ctx.clock._wall = DAY.timestamp()  # type: ignore[attr-defined]
    material_id = recorded_lithium_chain(ctx)
    with ctx.db.connect() as conn:
        rules = {r[0] for r in conn.execute("SELECT value FROM watch_rules WHERE active = 1")}
    assert "Yichun, Jiangxi" in rules or "Cluster de Yichun" in rules
    run_cycle(ctx.db, gdelt(ARTICLES), lambda name: None, DAY)
    backend = ReplayBatchBackend()
    triage.submit_pending(ctx.db, ctx.budget, backend)
    backend.responses = answers_for(ctx, lambda row: result(
        relevant=True, event_type="regulatory_change", event_status="ongoing", matched_node_ids=["yichun_cluster"],
        place="Yichun, Jiangxi", country_iso2="CN", event_date="2025-07-24", severity_hint=2,
        title_fr="Permis de huit mines de lithium de Yichun en révision", cited_origin=f"origine-{row['id']}"))
    triage.collect_pending(ctx.db, ctx.budget, backend)
    with ctx.db.transaction() as conn:
        (event_id,) = grouping.group_triaged(conn)
        event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    assert event["confirmation"] == "confirmed" and event["lineages"] == 2  # Reuters and Mining.com
    analyses = ReplayBatchBackend()
    assess.run_analyses(ctx.db, ctx.budget, None, analyses, DAY, Allowance())
    with ctx.db.connect() as conn:
        analyses.responses = {r["custom_id"]: message(analysis.split(impact(r["event_id"]))[r["part"]])
                              for r in conn.execute("SELECT p.custom_id, p.part, a.event_id FROM analysis_parts p "
                                                    "JOIN analyses a ON a.id = p.analysis_id")}
    assess.collect(ctx.db, ctx.budget, analyses)
    changed = dict(assess.refresh_verdicts(ctx.db, ctx.vault, DAY, verified=True, last_ok_cycle=DAY.isoformat()))
    v = changed[material_id]
    assert v.verdict in ("pot", "ant"), v.explanation  # at least « Problème potentiel » on 24 July
    assert v.event_ids == [event_id] and v.x_weeks is not None and v.x_weeks <= 2
