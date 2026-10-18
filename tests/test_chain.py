"""Phase 2: identification, AI call handling, validation, cross-check, lead times, persistence and routes."""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.chain import crosscheck, geo, leadtimes, service, store
from goulot.chain.countries import ISO3_TO_ISO2
from goulot.chain.identify import IdentificationError, identify, normalize_cas
from goulot.chain.quote import reconstruction_quote
from goulot.chain.research import build_report
from goulot.llm.backend import ReplayBackend
from goulot.llm.budget import Budget, BudgetExceeded, CallRecord, CircuitOpen
from goulot.llm.calls import CallSpec, LLMRefused, LLMTruncated, call
from goulot.llm.pricing import Usage, cost_usd
from goulot.llm.validate import check_chain
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from tests import chain_fixtures as fx
from tests.conftest import login, unsafe_headers


def mock_http(routes: dict[str, Any] | None = None, seen: list[httpx2.Request] | None = None) -> httpx2.Client:
    routes = fx.IDENTITY_ROUTES if routes is None else routes

    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        body = routes.get(request.url.path)
        if body is None:
            return httpx2.Response(404, json={})
        return httpx2.Response(200, json=body)

    return build_client(Allowlist(set()), transport=httpx2.MockTransport(handler))


def make_material(ctx: AppContext) -> str:
    identity = identify(mock_http(), fx.CAS)
    with ctx.db.transaction() as conn:
        return store.create_material(conn, identity.as_dict())


def run(ctx: AppContext, backend: ReplayBackend, budget_name: str = "reconstruction") -> tuple[str, str, float]:
    material_id = make_material(ctx)
    job_id = service.start_job(ctx.db, material_id)
    cost = service.reconstruct(service.Deps(ctx.db, ctx.budget, backend, None), material_id, job_id,
                               budget_name=budget_name)
    return material_id, job_id, cost


# --- identification --------------------------------------------------------------------------------------
def test_cas_check_digit() -> None:
    assert normalize_cas("107-13-1") == "107-13-1"
    assert normalize_cas("554-13-2") == "554-13-2"
    assert normalize_cas("7440-55-3") == "7440-55-3"
    assert normalize_cas("107-13-2") is None
    assert normalize_cas("abc") is None


def test_identify_by_cas_and_by_name() -> None:
    seen: list[httpx2.Request] = []
    by_cas = identify(mock_http(seen=seen), "107-13-1")
    assert (by_cas.cas, by_cas.name, by_cas.formula) == ("107-13-1", "Acrylonitrile", "C3H3N")
    assert by_cas.name_fr == "acrylonitrile"
    assert [s["name"] for s in by_cas.sources] == ["PubChem", "Wikidata"]
    assert all(s["url"].startswith("https://") for s in by_cas.sources)
    assert "107-13-1" not in by_cas.synonyms
    assert identify(mock_http(), "acrylonitrile").cas == "107-13-1"
    assert all(r.url.scheme == "https" for r in seen)


def test_identify_errors() -> None:
    with pytest.raises(IdentificationError, match="chiffre de contrôle"):
        identify(mock_http(), "107-13-2")
    with pytest.raises(IdentificationError, match="introuvable"):
        identify(mock_http(), "notasubstance")
    with pytest.raises(IdentificationError):
        identify(mock_http(), "x" * 101)


# --- pricing, budget, breaker ------------------------------------------------------------------------------
def test_cost_uses_pricing_and_web_searches() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, web_searches=20)
    assert cost_usd("claude-sonnet-5-5", usage) == pytest.approx(2.0 + 1.0 + 0.2)
    assert cost_usd("claude-sonnet-5-5", usage, batch=True) == pytest.approx(1.5 + 0.2)
    long_prompt = Usage(input_tokens=150_000, output_tokens=1_000)
    assert cost_usd("claude-haiku-5-5", long_prompt) == pytest.approx(0.075 + 0.0025)  # long-prompt rates


def test_quote_under_three_dollars() -> None:
    quote = reconstruction_quote()
    assert 0.5 < quote.estimate_usd < quote.max_usd < 3.0


def test_budget_cap_refuses_before_sending(ctx: AppContext) -> None:
    ctx.budget.record(CallRecord("reconstruction", "test", "claude-sonnet-5-5", "standard", Usage(), 9.5))
    backend = ReplayBackend(responses=fx.replay_responses())
    with pytest.raises(BudgetExceeded):
        call(backend, ctx.budget, CallSpec("k", "chain_research", "reconstruction", 1.0), {"model": "claude-sonnet-5-5",
                                                                                          "messages": []})
    assert backend.requests == []


def test_circuit_breaker_after_three_errors(ctx: AppContext) -> None:
    budget = Budget(ctx.db, ctx.clock)
    for _ in range(3):
        budget.record(CallRecord("dev", "test", "claude-sonnet-5-5", "standard", Usage(), 0.0, ok=False))
    with pytest.raises(CircuitOpen):
        budget.check("dev", 0.01)
    ctx.clock.advance(3601)  # type: ignore[attr-defined]
    budget.check("dev", 0.01)


def test_stop_reasons(ctx: AppContext) -> None:
    params = {"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "x"}]}
    refused = {**fx.structure_response(), "stop_reason": "refusal"}
    with pytest.raises(LLMRefused):
        call(ReplayBackend(responses={"k": refused}), ctx.budget, CallSpec("k", "t", "dev", 0.1), params)
    cut = {**fx.structure_response(), "stop_reason": "max_tokens"}
    with pytest.raises(LLMTruncated):
        call(ReplayBackend(responses={"k": cut}), ctx.budget, CallSpec("k", "t", "dev", 0.1), params)
    # pause_turn: the paused turn is sent back as an assistant message, then the answer is completed.
    backend = ReplayBackend(responses={"k": fx.research_response(stop_reason="pause_turn"),
                                       "k_cont1": fx.structure_response()})
    result = call(backend, ctx.budget, CallSpec("k", "t", "dev", 1.0), params)
    assert len(backend.requests) == 2
    assert backend.requests[1][1]["messages"][-1]["role"] == "assistant"
    assert result.usage.web_searches == 6
    with ctx.db.connect() as conn:
        rows = conn.execute("SELECT stop_reason, request_id FROM llm_calls ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["refusal", "max_tokens", "pause_turn", "end_turn"]
    assert rows[-1][1] == "req_structure"


def test_live_backend_returns_json_safe_answers(tmp_path: Any) -> None:
    """A real answer can hold datetimes (container expiry of the web-search filtering): recording must work."""
    from datetime import UTC, datetime

    import anthropic

    from goulot.llm.backend import AnthropicBackend, RecordingBackend

    message = anthropic.types.Message.model_validate({
        **fx.structure_response(), "content": [{"type": "text", "text": "{}"}],
        "container": {"id": "container_1", "expires_at": datetime(2026, 10, 16, tzinfo=UTC)},
    })

    class FakeStream:
        def __enter__(self) -> FakeStream:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def get_final_message(self) -> anthropic.types.Message:
            return message

    class FakeClient:
        class messages:
            @staticmethod
            def stream(**_: Any) -> FakeStream:
                return FakeStream()

    recorder = RecordingBackend(AnthropicBackend(FakeClient()), tmp_path)  # type: ignore[arg-type]
    answer = recorder.create("k", {"model": "claude-sonnet-5-5"})
    assert answer["container"]["expires_at"].startswith("2026-10-16")
    assert ReplayBackend(tmp_path).create("k", {})["container"]["id"] == "container_1"


def test_resume_reuses_paid_answers_without_billing(ctx: AppContext, tmp_path: Any) -> None:
    import anthropic

    from goulot.llm.backend import RecordingBackend, ResumingBackend

    class StructureFails(ReplayBackend):
        def create(self, key: str, params: dict[str, Any]) -> dict[str, Any]:
            if key.startswith("chain_structure"):
                raise anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
            return super().create(key, params)

    first = RecordingBackend(StructureFails(responses={f"chain_research_{fx.CAS}": fx.research_response()}), tmp_path)
    material_id, job_id, _ = run(ctx, first)  # research paid and recorded, structuring failed
    assert (service.job_status(ctx.db, job_id) or {})["state"] == "failed"
    paid = ctx.budget.spent("reconstruction")
    live = ReplayBackend(responses={f"chain_structure_{fx.CAS}": fx.structure_response(),
                                    f"chain_context_{fx.CAS}": fx.context_response()})
    with ctx.db.transaction() as conn:
        store.set_status(conn, material_id, "draft")
    job_id = service.start_job(ctx.db, material_id)
    service.reconstruct(service.Deps(ctx.db, ctx.budget, ResumingBackend(RecordingBackend(live, tmp_path), tmp_path),
                                     None), material_id, job_id)
    assert (service.job_status(ctx.db, job_id) or {})["state"] == "done"
    assert [k for k, _ in live.requests] == [f"chain_structure_{fx.CAS}", f"chain_context_{fx.CAS}"]  # no research
    structure_cost = sum(cost_usd("claude-sonnet-5-5", Usage.from_api(r["usage"]))
                         for r in (fx.structure_response(), fx.context_response()))
    assert ctx.budget.spent("reconstruction") == pytest.approx(paid + structure_cost)


def test_api_error_reason_is_shown(ctx: AppContext) -> None:
    import anthropic

    class Failing:
        def create(self, key: str, params: dict[str, Any]) -> dict[str, Any]:
            request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            response = httpx2.Response(400, request=request)
            raise anthropic.BadRequestError("bad", response=response, body={
                "type": "error", "error": {"type": "invalid_request_error", "message": "Schema is too complex"}})

    with pytest.raises(LLMRefused.__mro__[1], match="HTTP 400, Schema is too complex"):
        call(Failing(), ctx.budget, CallSpec("k", "t", "dev", 0.1), {"model": "claude-sonnet-5-5", "messages": []})


def test_sdk_timeout_is_not_the_sources_timeout() -> None:
    from goulot.llm.client import make_anthropic_client

    client = make_anthropic_client("sk-ant-test-0000000000000000", mock_http({}))
    assert client.timeout.read == 300 and client.timeout.connect == 10  # type: ignore[union-attr]


def test_research_without_citation_stops_before_structuring(ctx: AppContext) -> None:
    research = fx.research_response()
    for block in research["content"]:
        block.pop("citations", None)
    backend = ReplayBackend(responses={**fx.replay_responses(), f"chain_research_{fx.CAS}": research})
    _, job_id, _ = run(ctx, backend)
    job = service.job_status(ctx.db, job_id) or {}
    assert job["state"] == "failed" and "aucune source" in job["message"]
    assert [k for k, _ in backend.requests] == [f"chain_research_{fx.CAS}"]


def test_pause_turn_resumes_in_same_container(ctx: AppContext) -> None:
    paused = {**fx.research_response(stop_reason="pause_turn"), "container": {"id": "container_9"}}
    backend = ReplayBackend(responses={"k": paused, "k_cont1": fx.structure_response()})
    call(backend, ctx.budget, CallSpec("k", "t", "dev", 1.0), {"model": "claude-sonnet-5-5", "messages": []})
    assert "container" not in backend.requests[0][1] and backend.requests[1][1]["container"] == "container_9"


# --- research report ------------------------------------------------------------------------------------
def test_report_markers_and_sources() -> None:
    report, sources = build_report(fx.research_response()["content"])
    assert [s["ref"] for s in sources] == ["src1", "src2", "src3", "src4", "src5"]
    assert all(s["url"].startswith("https://") for s in sources)
    assert sources[0]["publisher"] == "pubs.usgs.gov" and sources[0]["page_age"] == "2026-03-01"
    assert "[src1]" in report and "[src2] [src3]" in report
    assert "http" not in report and "phish" not in report and "[src9]" not in report


# --- validation of the chain ------------------------------------------------------------------------------
def test_example_chain_passes_checks() -> None:
    check = check_chain(fx.example_chain(), {f"src{i}" for i in range(1, 5)}, fx.CAS)
    assert not check.fatal and not check.nodes and not check.edges and not check.general


def test_out_of_list_values_normalized() -> None:
    chain = fx.example_chain()
    chain["nodes"][5]["node_type"] = "Raffinerie"
    chain["nodes"][6]["confidence"] = "HIGH"
    chain["edges"][0]["transport_mode"] = "teleport"
    check = check_chain(chain, {f"src{i}" for i in range(1, 5)}, fx.CAS)
    assert chain["nodes"][5]["node_type"] == "other" and "plant_gulf_cracker" in check.nodes
    assert chain["nodes"][6]["confidence"] == "high" and "cn_acrylonitrile" not in check.nodes
    assert chain["edges"][0]["transport_mode"] == "unknown" and 0 in check.edges


def test_chain_anomalies() -> None:
    chain = fx.example_chain()
    assert check_chain(chain, {"src1"}, "554-13-2").fatal  # CAS changed
    chain = copy.deepcopy(fx.example_chain())
    chain["nodes"][6]["global_share_pct"] = 140
    chain["nodes"][7]["source_ids"] = ["src99"]
    chain["edges"].append({"from_id": "acrylonitrile", "to_id": "propylene", "relation": "supplies",
                           "transport_mode": "road", "lead_time_days": None, "share_pct": None,
                           "confidence": "low", "source_ids": []})
    check = check_chain(chain, {f"src{i}" for i in range(1, 5)}, fx.CAS)
    assert not check.fatal
    assert "cn_acrylonitrile" in check.nodes and "us_acrylonitrile" in check.nodes
    assert "acrylonitrile" in check.nodes and any("cycle" in g for g in check.general)


# --- lead times, geography, cross-check ------------------------------------------------------------------
def test_lead_time_defaults() -> None:
    chain = fx.example_chain()
    leadtimes.fill_defaults(chain)
    nodes = {n["node_id"]: n for n in chain["nodes"]}
    assert nodes["port_rotterdam"]["buffer_days"] == 21 and not nodes["port_rotterdam"]["buffer_is_default"]
    assert nodes["cn_acrylonitrile"]["buffer_days"] == 21 and nodes["cn_acrylonitrile"]["buffer_is_default"]
    assert nodes["plant_gulf_cracker"]["buffer_days"] == 21 and nodes["acrylonitrile"]["buffer_days"] == 0
    assert nodes["chokepoint_suez"]["buffer_days"] == 0
    edges = {(e["from_id"], e["to_id"]): e for e in chain["edges"]}
    assert edges[("cn_acrylonitrile", "chokepoint_suez")]["lead_time_days"] == 30  # no located ports
    assert edges[("chokepoint_suez", "port_rotterdam")]["lead_time_basis"] == "source"
    assert edges[("port_rotterdam", "waterway_rhine")]["lead_time_days"] == 5
    assert edges[("naphtha_propane", "propylene")]["lead_time_days"] == 0


def test_producer_straight_to_purchased_material_gets_a_delivery_delay() -> None:
    chain = fx.example_chain()
    leadtimes.fill_defaults(chain)
    de = next(e for e in chain["edges"] if e["from_id"] == "de_acrylonitrile")
    assert (de["transport_mode"], de["lead_time_days"], de["lead_time_basis"]) == ("unknown", 7, "default")
    feed = next(e for e in chain["edges"] if e["from_id"] == "naphtha_propane")
    assert feed["lead_time_days"] == 0  # a feedstock link has no transport


def test_sea_estimate_between_ports() -> None:
    shanghai = {"node_type": "port", "lat": 31.23, "lon": 121.47}
    rotterdam = {"node_type": "port", "lat": 51.95, "lon": 4.14}
    days = leadtimes.sea_days(shanghai, rotterdam)
    assert 20 < days < 40  # about 9,000 km great circle × 1.3 at 13 knots


def test_country_from_coordinates() -> None:
    square = [{"iso2": "FR", "bbox": [0, 40, 10, 50], "polygons": [[[0, 40], [10, 40], [10, 50], [0, 50], [0, 40]]]}]
    chain = {"nodes": [
        {"node_id": "a", "node_type": "plant", "country_iso2": "DE", "lat": 45.0, "lon": 5.0, "confidence": "high"},
        {"node_id": "b", "node_type": "plant", "country_iso2": "FR", "lat": 45.0, "lon": 5.0, "confidence": "high"},
        {"node_id": "c", "node_type": "port", "country_iso2": "NL", "lat": 60.0, "lon": 5.0, "confidence": "high"},
    ]}
    assert geo.check_coordinates(chain, square) == 1
    assert chain["nodes"][0]["lat"] is None and chain["nodes"][0]["confidence"] == "low"
    assert chain["nodes"][1]["lat"] == 45.0 and chain["nodes"][2]["lat"] == 60.0


def test_iso_codes() -> None:
    assert ISO3_TO_ISO2["CHN"] == "CN" and ISO3_TO_ISO2["USA"] == "US" and ISO3_TO_ISO2["DEU"] == "DE"
    assert len(ISO3_TO_ISO2) == len(set(ISO3_TO_ISO2.values())) >= 245


def test_comtrade_crosscheck_flags_gap() -> None:
    rows = [{"reporterISO": "CHN", "partnerCode": 0, "primaryValue": 200.0},
            {"reporterISO": "USA", "partnerCode": 0, "primaryValue": 600.0},
            {"reporterISO": "DEU", "partnerCode": 0, "primaryValue": 200.0}]
    seen: list[httpx2.Request] = []
    client = mock_http({"/public/v1/preview/C/A/HS": {"data": rows}}, seen)
    ref = crosscheck.comtrade_export_shares(client, "292610", 2024)
    assert ref is not None and ref.shares == {"CN": 20.0, "US": 60.0, "DE": 20.0}
    chain = fx.example_chain()
    assert crosscheck.apply_reference(chain, ref, tier=1) == 2  # CN 40 vs 20, US 15 vs 60
    cn = next(n for n in chain["nodes"] if n["node_id"] == "cn_acrylonitrile")
    assert cn["confidence"] == "low" and "30 %" in cn["anomalies"][0]
    keyed: list[httpx2.Request] = []
    crosscheck.comtrade_export_shares(mock_http({}, keyed), "292610", 2024, api_key="k" * 32)
    assert keyed[0].headers["ocp-apim-subscription-key"] == "k" * 32 and "k" * 32 not in str(keyed[0].url)
    assert crosscheck.comtrade_export_shares(client, "29261", 2024) is None


def test_usgs_parser() -> None:
    csv_text = ("Source,Section,COMMODITY,COUNTRY,TYPE,UNIT_MEAS,PROD_2024,PROD_EST_2025\n"
                "MCS,World,Gallium,China,Primary,t,750,800\n"
                "MCS,World,Gallium,Russia,Primary,t,5,5\n"
                "MCS,World,Gallium,World total (rounded),Primary,t,760,810\n")
    ref = crosscheck.usgs_production_shares(csv_text, "gallium", "https://www.usgs.gov/x")
    assert ref is not None and ref.shares["CN"] == pytest.approx(98.77, abs=0.01)


# --- end to end with recorded answers ----------------------------------------------------------------------
def test_reconstruction_end_to_end(ctx: AppContext) -> None:
    backend = ReplayBackend(responses=fx.replay_responses())
    material_id, job_id, cost = run(ctx, backend)
    job = service.job_status(ctx.db, job_id)
    assert job and job["state"] == "done", job
    with ctx.db.connect() as conn:
        material = store.get_material(conn, material_id)
        chain = store.load_chain(conn, material_id)
        calls = conn.execute("SELECT purpose, cost_usd, web_searches FROM llm_calls ORDER BY id").fetchall()
    assert material and material["status"] == "review"
    assert material["hs_codes"] == [{"code": "292610", "origin": "ai", "verified": False}]
    assert len(chain["nodes"]) == 12 and len(chain["routes"]) == 1
    assert {s["url"] for s in chain["sources"]} >= {fx.SOURCES[0][0]}
    assert all(n["confidence"] in ("high", "medium", "low") for n in chain["nodes"])
    assert [c[0] for c in calls] == ["chain_research", "chain_structure", "chain_context"] and calls[0][2] == 6
    assert cost == pytest.approx(sum(c[1] for c in calls)) and cost < 3
    assert any("Natural Earth" in q for q in chain["open_questions"]) != geo.borders_available()


def test_schema_failure_retried_once(ctx: AppContext) -> None:
    responses = fx.replay_responses()
    responses[f"chain_structure_{fx.CAS}"] = fx.structure_response(text="not json")
    backend = ReplayBackend(responses={**responses, f"chain_structure_{fx.CAS}_retry1": fx.structure_response()})
    _, job_id, _ = run(ctx, backend)
    assert (service.job_status(ctx.db, job_id) or {})["state"] == "done"
    backend = ReplayBackend(responses={**responses, f"chain_structure_{fx.CAS}_retry1":
                                       fx.structure_response(text="{}")})
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE materials SET status = 'draft'")
    _, job_id, _ = run(ctx, backend)
    job = service.job_status(ctx.db, job_id) or {}
    assert job["state"] == "failed" and "schéma" in job["message"]


def test_context_failure_keeps_the_chain(ctx: AppContext) -> None:
    responses = fx.replay_responses()
    responses[f"chain_context_{fx.CAS}"] = fx.context_response(text="not json")
    material_id, job_id, _ = run(ctx, ReplayBackend(responses=responses))
    assert (service.job_status(ctx.db, job_id) or {})["state"] == "done"
    with ctx.db.connect() as conn:
        chain = store.load_chain(conn, material_id)
    assert len(chain["nodes"]) == 12 and chain["routes"] == []
    assert any("non structurés" in q for q in chain["open_questions"])


def test_context_node_ids_checked(ctx: AppContext) -> None:
    chain = fx.structured_chain()
    chain["structural_risks"][0]["node_ids"] = ["invented_node"]
    responses = {**fx.replay_responses(), f"chain_context_{fx.CAS}": fx.context_response(chain)}
    material_id, _, _ = run(ctx, ReplayBackend(responses=responses))
    with ctx.db.connect() as conn:
        assert any("maillon inconnu" in q for q in store.load_chain(conn, material_id)["open_questions"])


def test_purchased_material_coproducts_detached() -> None:
    from goulot.chain.structure import detach_purchased_coproducts

    chain = fx.structured_chain()
    chain["nodes"].append({**chain["nodes"][1], "node_id": "hcn", "name": "Acide cyanhydrique", "tier": 1})
    chain["edges"].append({"from_id": "acrylonitrile", "to_id": "hcn", "relation": "yields_coproduct",
                           "transport_mode": "none", "lead_time_days": None, "share_pct": None,
                           "confidence": "medium", "source_ids": ["src1"]})
    assert detach_purchased_coproducts(chain) == {"hcn"}
    assert all(e["from_id"] != "acrylonitrile" for e in chain["edges"])
    assert "hcn" not in {n["node_id"] for n in chain["nodes"]}
    assert "Acide cyanhydrique" in chain["nodes"][0]["notes"]
    check = check_chain(chain, {f"src{i}" for i in range(1, 5)}, fx.CAS)
    assert "acrylonitrile" not in check.nodes


def test_correction_is_bitemporal(ctx: AppContext) -> None:
    material_id, _, _ = run(ctx, ReplayBackend(responses=fx.replay_responses()))
    with ctx.db.transaction() as conn:
        store.correct_node(conn, material_id, "port_rotterdam", {"buffer_days": 10.0})
    with ctx.db.connect() as conn:
        rows = conn.execute("SELECT buffer_days, buffer_is_default, valid_to, user_modified FROM chain_nodes "
                            "WHERE node_key = 'port_rotterdam' ORDER BY id").fetchall()
        current = store.load_chain(conn, material_id)
    assert len(rows) == 2 and rows[0][2] is not None and rows[1][2] is None
    assert (rows[1][0], rows[1][1], rows[1][3]) == (10.0, 0, 1)
    assert sum(1 for n in current["nodes"] if n["node_key"] == "port_rotterdam") == 1
    with pytest.raises(store.ChainStateError), ctx.db.transaction() as conn:
        store.correct_node(conn, material_id, "port_rotterdam", {"tier": 0})


# --- routes ---------------------------------------------------------------------------------------------
def test_routes_add_quote_reconstruct_validate(client: TestClient, ctx: AppContext) -> None:
    backend = ReplayBackend(responses=fx.replay_responses())
    ctx.llm_backend = lambda: backend
    ctx.http_client = lambda: mock_http()
    csrf = login(client, ctx)
    h = unsafe_headers(csrf)
    r = client.post("/api/materials/identify", json={"query": "107-13-1"}, headers=h)
    assert r.status_code == 200 and r.json()["formula"] == "C3H3N"
    assert client.post("/api/materials/identify", json={"query": "107-13-2"}, headers=h).status_code == 422
    material = client.post("/api/materials", json={"query": "acrylonitrile"}, headers=h).json()
    mid = material["id"]
    assert material["status"] == "draft"
    quote = client.post(f"/api/materials/{mid}/quote", json={}, headers=h).json()
    assert quote["max_usd"] < 3 and quote["cap_usd"] == 10
    assert client.post(f"/api/materials/{mid}/reconstruct", json={"quote_id": "0" * 36}, headers=h).status_code == 409
    job = client.post(f"/api/materials/{mid}/reconstruct", json={"quote_id": quote["quote_id"]}, headers=h).json()
    ctx.jobs.shutdown(wait=True)
    assert client.get(f"/api/jobs/{job['job_id']}").json()["state"] == "done"
    again = client.post(f"/api/materials/{mid}/reconstruct", json={"quote_id": quote["quote_id"]}, headers=h)
    assert again.status_code == 409  # a quote is used once
    chain = client.get(f"/api/materials/{mid}/chain").json()
    assert chain["material"]["status"] == "review" and len(chain["nodes"]) == 12
    delay = {n["node_key"]: n["delay"] for n in chain["nodes"]}
    assert delay["acrylonitrile"] == {"days": 0, "low": 0, "high": 0, "buffers": 0}
    # Rotterdam: Rhine barge 5 d + Rhine 0 d buffer + last leg 5 d = 10 d; its own buffer is not counted.
    assert delay["port_rotterdam"]["days"] == 10
    # US producers: sea 18 d + Rotterdam buffer 21 d (given, so the same in every bound) + 10 d.
    assert delay["us_acrylonitrile"] == {"days": 49, "low": 49, "high": 49, "buffers": 21}  # Rotterdam's stock
    r = client.patch(f"/api/materials/{mid}/nodes/port_rotterdam", json={"buffer_days": 9}, headers=h)
    assert r.status_code == 200
    assert client.patch(f"/api/materials/{mid}/nodes/port_rotterdam", json={"tier": 2}, headers=h).status_code == 422
    assert client.post(f"/api/materials/{mid}/validate", json={}, headers=h).json() == {"ok": True}
    assert client.get(f"/api/materials/{mid}").json()["status"] == "validated"
    spend = client.get("/api/llm/spend").json()
    assert spend["reconstruction"]["spent_usd"] > 0 and "dev" not in spend


def test_recorded_fixtures_meet_phase_2_criteria(ctx: AppContext) -> None:
    """Replays every recording made by `goulot dev record` (none before the user runs it)."""
    from goulot.cli.dev import FIXTURES, TEST_CAS

    for directory in sorted(p for p in FIXTURES.glob("*") if (p / "summary.json").is_file()
                            and not p.name.startswith("watch_")):  # the watch scenario has its own test
        assert directory.name in TEST_CAS
        identity = json.loads((directory / "identity.json").read_text(encoding="utf-8"))
        with ctx.db.transaction() as conn:
            material_id = store.create_material(conn, identity)
        job_id = service.start_job(ctx.db, material_id)
        cost = service.reconstruct(service.Deps(ctx.db, ctx.budget, ReplayBackend(directory), None), material_id,
                                   job_id, budget_name="dev")
        assert (service.job_status(ctx.db, job_id) or {})["state"] == "done", directory.name
        with ctx.db.connect() as conn:
            chain = store.load_chain(conn, material_id)
        assert chain["routes"], directory.name
        assert any(n["country_iso2"] != "unknown" for n in chain["nodes"]), directory.name
        assert any(n["node_type"] in ("mine", "plant", "recycler") and n["source_refs"] for n in chain["nodes"])
        assert all(n["confidence"] in ("high", "medium", "low") for n in chain["nodes"])
        assert cost < 3, directory.name
