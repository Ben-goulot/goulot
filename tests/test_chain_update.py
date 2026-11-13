"""Chain freshness (2026-11-11): status and share basis of each node, like-with-like cross-check, USGS by commodity,
and the light update of a chain (re-reading the stored report or searching what changed), proposal by proposal.

Model answers are hand-written and fictitious (test CAS 107-13-1 only); no network, no real keychain.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.chain import crosscheck, research, service, store, structure, update
from goulot.llm.backend import ReplayBackend
from goulot.llm.validate import check_chain
from goulot.settings import REPO_ROOT
from tests import chain_fixtures as fx
from tests.conftest import login, unsafe_headers
from tests.test_chain import mock_http, run

TODAY = "2026-11-11"


def message(body: Any, request_id: str = "upd") -> dict[str, Any]:
    return {
        "id": f"msg_{request_id}", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "stop_reason": "end_turn", "_request_id": f"req_{request_id}",
        "content": [{"type": "text", "text": body if isinstance(body, str) else json.dumps(body)}],
        "usage": fx.usage(30_000, 6_000),
    }


def example_update() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (REPO_ROOT / "schemas" / "examples" / "chain_update_v1.example.json").read_text(encoding="utf-8"))
    return data


def validated_chain(ctx: AppContext) -> str:
    material_id, _, _ = run(ctx, ReplayBackend(responses=fx.replay_responses()))
    with ctx.db.transaction() as conn:
        store.validate_material(conn, material_id, acknowledge_injection=True)
    return material_id


def reread(ctx: AppContext, material_id: str, output: dict[str, Any]) -> dict[str, Any]:
    job_id = update.start_job(ctx.db, material_id)
    backend = ReplayBackend(responses={f"chain_update_reread_{fx.CAS}": message(output)})
    update.run_update(ctx.db, ctx.budget, backend, material_id, job_id, "reread", today=TODAY)
    job = service.job_status(ctx.db, job_id) or {}
    assert job["state"] == "done", job
    assert backend.requests[0][1]["output_config"]["format"]["schema"]["properties"]["schema_version"]["const"] \
        == "update-1.0"
    with ctx.db.connect() as conn:
        return update.overview(conn, material_id)


def node(ctx: AppContext, material_id: str, key: str) -> dict[str, Any] | None:
    with ctx.db.connect() as conn:
        return next((n for n in store.load_chain(conn, material_id)["nodes"] if n["node_key"] == key), None)


def rules_for(ctx: AppContext, material_id: str, key: str) -> list[str]:
    with ctx.db.connect() as conn:
        return [r[0] for r in conn.execute("SELECT value FROM watch_rules WHERE material_id = ? AND node_key = ? "
                                           "AND active = 1", (material_id, key))]


# --- structuring v3 ----------------------------------------------------------------------------------------------
def test_structuring_uses_v3_and_keeps_basis_and_status(ctx: AppContext) -> None:
    backend = ReplayBackend(responses=fx.replay_responses())
    material_id, _, _ = run(ctx, backend)
    params = dict(backend.requests)[f"chain_structure_{fx.CAS}"]
    assert params["system"][0]["text"].startswith("Tu convertis") and "Aucun producteur oublié" in \
        params["system"][0]["text"]
    assert params["output_config"]["format"]["schema"]["properties"]["schema_version"]["const"] == "core-2.0"
    cn = node(ctx, material_id, "cn_acrylonitrile")
    plant = node(ctx, material_id, "plant_gulf_cracker")
    assert cn and (cn["share_basis"], cn["share_year"]) == ("capacity", 2025)
    assert plant and plant["status"] == "operating"


def test_research_gets_todays_date() -> None:
    params = research.research_params({"name": "x", "cas": fx.CAS, "formula": "", "purchased_form": "x"}, TODAY)
    assert "<today>\n2026-11-11\n</today>" in params["messages"][0]["content"]
    assert "actuelle" in params["system"]


def test_answer_recorded_before_v3_is_still_read() -> None:
    core = fx.core_part(fx.structured_chain())
    core["schema_version"] = "core-1.0"
    for n in core["nodes"]:
        for key in ("status", "share_basis", "share_year"):
            n.pop(key)
    parsed = structure.parse_core([{"type": "text", "text": json.dumps(core)}])
    assert parsed["legacy_shares"] and all(n["status"] == "unknown" for n in parsed["nodes"])
    chain = {**parsed, "routes": [], "substitutes": [], "structural_risks": [], "upcoming_dates": []}
    check = check_chain(chain, {f"src{i}" for i in range(1, 6)}, fx.CAS)
    assert not any("nature" in m for msgs in check.nodes.values() for m in msgs)  # old answers: not their fault


def test_share_without_basis_or_on_a_closed_site_is_flagged() -> None:
    chain = fx.structured_chain()
    cn = next(n for n in chain["nodes"] if n["node_id"] == "cn_acrylonitrile")
    cn["share_basis"] = "unknown"
    cn["share_year"] = 1850
    plant = next(n for n in chain["nodes"] if n["node_id"] == "plant_gulf_cracker")
    plant.update({"status": "closed", "global_share_pct": 3.0, "share_basis": "production", "share_year": 2025})
    de = next(n for n in chain["nodes"] if n["node_id"] == "de_acrylonitrile")
    de.update({"share_basis": "production", "share_year": 2024})  # no share: basis and year dropped
    check = check_chain(chain, {f"src{i}" for i in range(1, 6)}, fx.CAS)
    assert any("sans nature" in m for m in check.nodes["cn_acrylonitrile"]) and cn["share_year"] is None
    assert any("fermé ou en projet" in m for m in check.nodes["plant_gulf_cracker"])
    assert (de["share_basis"], de["share_year"]) == ("unknown", None) and "de_acrylonitrile" not in check.nodes


# --- cross-check -------------------------------------------------------------------------------------------------
def test_usgs_by_commodity_and_note_when_not_configured() -> None:
    assert crosscheck.usgs_commodity("Concentré de spodumène (SC6)") == "lithium"
    assert crosscheck.usgs_commodity("Alumine (liqueur Bayer)") == "alumina"
    assert crosscheck.usgs_commodity("Acrylonitrile") is None
    csv_text = ("COMMODITY,COUNTRY,TYPE,PROD_2024,PROD_EST_2025\n"
                "Bauxite and alumina,Guinea,Mine production: bauxite,130000,150000\n"
                "Bauxite and alumina,Australia,Mine production: bauxite,100000,100000\n"
                "Bauxite and alumina,World total (rounded),Mine production: bauxite,400000,440000\n"
                "Bauxite and alumina,China,Refinery production: alumina,84000,90000\n"
                "Bauxite and alumina,World total (rounded),Refinery production: alumina,140000,150000\n")
    ref = crosscheck.usgs_production_shares(csv_text, "bauxite", "https://www.usgs.gov/x")
    assert ref is not None and ref.basis == "production" and ref.shares == {"GN": 34.09, "AU": 22.73}
    alumina = crosscheck.usgs_production_shares(csv_text, "alumina", "https://www.usgs.gov/x")
    assert alumina is not None and alumina.shares == {"CN": 60.0}
    chain = {"nodes": [
        {"node_id": "gn", "node_type": "country", "tier": 3, "country_iso2": "GN", "product": "Bauxite",
         "name": "Guinée (bauxite)", "global_share_pct": 34.0, "share_basis": "production", "confidence": "medium",
         "lat": None, "lon": None},
        {"node_id": "cn", "node_type": "country", "tier": 2, "country_iso2": "CN", "product": "Alumine",
         "name": "Chine (alumine)", "global_share_pct": 20.0, "share_basis": "unknown", "confidence": "medium",
         "lat": None, "lon": None}],
        "material": {"hs_code_candidates": []}}
    notes = service.cross_check(service.Deps(None, None, None, None, usgs_csv=csv_text),  # type: ignore[arg-type]
                                chain, {"name": "gallium"})
    gn, cn = chain["nodes"]
    assert gn["confidence"] == "medium" and gn["crosscheck"][0]["share_pct"] == 34.09
    assert cn["confidence"] == "low" and "60.0 %" in cn["anomalies"][0]
    assert not any("USGS" in n for n in notes)
    plain = copy.deepcopy(chain)
    notes = service.cross_check(service.Deps(None, None, None, None), plain, {"name": "gallium"})  # type: ignore[arg-type]
    assert any("Recoupement USGS non fait" in n for n in notes)


def test_usgs_data_url_host_joins_the_allowlist(tmp_path: Any) -> None:
    from goulot.net.allowlist import source_hosts

    path = tmp_path / "sources.yaml"
    path.write_text("sources:\n  - id: usgs_mcs\n    enabled: true\n    url: https://www.usgs.gov/x\n"
                    "    data_url: https://www.sciencebase.gov/catalog/file/get/abc\n", encoding="utf-8")
    assert {"www.usgs.gov", "www.sciencebase.gov"} <= source_hosts(path)
    path.write_text("sources:\n  - id: usgs_mcs\n    enabled: true\n    url: https://www.usgs.gov/x\n"
                    "    data_url: http://www.sciencebase.gov/x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        source_hosts(path)


def test_export_shares_stay_out_of_the_compliance_index() -> None:
    from goulot.compliance.concentration import national_shares

    nodes = [{"node_type": "country", "country_iso2": "CL", "global_share_pct": 74.5, "share_basis": "exports"},
             {"node_type": "country", "country_iso2": "AU", "global_share_pct": 32.0, "share_basis": "production"}]
    assert national_shares(nodes) == {"AU": 32.0}


# --- light update ------------------------------------------------------------------------------------------------
def test_quotes() -> None:
    reread_q, search_q = update.update_quote("reread"), update.update_quote("search")
    assert reread_q.searches_max == 0 and reread_q.max_usd < 0.6 and reread_q.estimate_usd < 0.25
    assert search_q.searches_max == 6 and search_q.estimate_usd < 0.8 and search_q.max_usd < 1.5
    with pytest.raises(update.UpdateError):
        update.update_quote("everything")


def test_reread_proposals_then_decisions(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    output = example_update()
    for change in output["changes"]:
        change["source_ids"] = ["src2"]
    view = reread(ctx, material_id, output)
    items = {i["change_type"]: i for i in view["items"]}
    assert set(items) == {"status", "share", "new_producer", "date"} and view["freshness"]["pending"] == 4
    assert view["last_update"]["kind"] == "reread" and view["last_update"]["notes"] == output["open_questions"]
    before = node(ctx, material_id, "cn_acrylonitrile")
    with ctx.db.transaction() as conn:
        update.decide(conn, material_id, items["status"]["id"], True)
        update.decide(conn, material_id, items["share"]["id"], True)
        update.decide(conn, material_id, items["new_producer"]["id"], True)
        update.decide(conn, material_id, items["date"]["id"], False)
        with pytest.raises(update.UpdateError):
            update.decide(conn, material_id, items["date"]["id"], True)  # already decided
    plant = node(ctx, material_id, "plant_gulf_cracker")
    cn = node(ctx, material_id, "cn_acrylonitrile")
    new = node(ctx, material_id, "plant_exempleville")
    assert plant and plant["status"] == "idled" and plant["user_modified"] == 1
    assert before and cn and (cn["global_share_pct"], cn["share_basis"], cn["share_year"]) == (45, "capacity", 2026)
    assert "src2" in cn["source_refs"]
    assert new and new["tier"] == 1 and new["country_iso2"] == "KR" and new["status"] == "operating"
    assert new["source_refs"] == ["src2"] and "Usine d'Exempleville" in rules_for(ctx, material_id, "plant_exempleville")
    with ctx.db.connect() as conn:
        chain = store.load_chain(conn, material_id)
        history = conn.execute("SELECT COUNT(*) FROM chain_nodes WHERE material_id = ? AND node_key = "
                               "'cn_acrylonitrile'", (material_id,)).fetchone()[0]
        assert update.freshness(conn, material_id)["pending"] == 0
    assert history == 2  # bitemporal: the old row is closed, not overwritten
    assert any(e["from_key"] == "plant_exempleville" and e["to_key"] == "acrylonitrile"
               and e["transport_mode"] == "unknown" for e in chain["edges"])
    assert not any(d["date"] == "2027-01" for d in chain["dates"])  # rejected


def test_closed_site_is_kept_but_not_watched(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    assert rules_for(ctx, material_id, "plant_gulf_cracker")
    output = example_update()
    output["changes"] = [{**output["changes"][0], "status": "closed", "source_ids": ["src1"]}]
    item = reread(ctx, material_id, output)["items"][0]
    with ctx.db.transaction() as conn:
        update.decide(conn, material_id, item["id"], True)
    plant = node(ctx, material_id, "plant_gulf_cracker")
    assert plant and plant["status"] == "closed" and rules_for(ctx, material_id, "plant_gulf_cracker") == []


def test_unfounded_proposals_are_dropped(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    base = example_update()["changes"]
    status, share, producer, date = (copy.deepcopy(c) for c in base)
    bad = [
        {**status, "node_id": "invented_plant", "source_ids": ["src1"]},
        {**status, "source_ids": []},
        {**status, "source_ids": ["src99"]},
        {**status, "status": "operating", "source_ids": ["src1"]},  # already operating
        {**share, "share_pct": 140, "source_ids": ["src1"]},
        {**share, "share_basis": "unknown", "source_ids": ["src1"]},
        {**producer, "new_name": "Usine fictive du golfe", "source_ids": ["src1"]},  # hypothetical duplicate below
        {**producer, "feeds_node_id": "port_rotterdam", "source_ids": ["src1"]},
        {**producer, "new_name": "Usine fermée d'Exempleville", "node_id": "plant_closed", "status": "closed",
         "source_ids": ["src1"]},
        {**date, "date": "2026-03", "source_ids": ["src1"]},
        {**date, "date": "bientôt", "source_ids": ["src1"]},
    ]
    with ctx.db.connect() as conn:
        names = [n["name"] for n in store.load_chain(conn, material_id)["nodes"]]
    bad[6]["new_name"] = names[0].upper()  # same name as a node already in the chain
    view = reread(ctx, material_id, {**example_update(), "changes": bad, "open_questions": []})
    assert view["items"] == [] and len(view["last_update"]["notes"]) == len(bad)


def test_search_update_adds_its_sources_after_the_stored_ones(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    url = "https://www.example-producer.com/news/2026-06-plant"

    def cite(text: str) -> dict[str, Any]:
        return {"type": "text", "text": text, "citations": [
            {"type": "web_search_result_location", "url": url, "title": "Mise en service", "cited_text": "x",
             "encrypted_index": "x"}]}

    research_answer = {**fx.research_response(), "content": [
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "acrylonitrile 2026"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1", "content": [
            {"type": "web_search_result", "url": url, "title": "Mise en service", "page_age": "2026-06-30",
             "encrypted_content": "x"}]},
        cite("## 2. Nouveaux producteurs\nUne usine fictive a démarré en juin 2026.")]}
    output = example_update()
    output["changes"] = [{**output["changes"][2], "source_ids": ["src6"]}]
    backend = ReplayBackend(responses={f"chain_update_research_{fx.CAS}": research_answer,
                                       f"chain_update_search_{fx.CAS}": message(output)})
    job_id = update.start_job(ctx.db, material_id)
    with pytest.raises(update.UpdateError):
        update.start_job(ctx.db, material_id)  # one job at a time
    update.run_update(ctx.db, ctx.budget, backend, material_id, job_id, "search", today=TODAY)
    assert (service.job_status(ctx.db, job_id) or {})["step_label"] == "Propositions"
    research_params = dict(backend.requests)[f"chain_update_research_{fx.CAS}"]
    user = research_params["messages"][0]["content"]
    assert "<since>" in user and "plant_gulf_cracker | plant |" in user
    assert research_params["tools"][0]["max_uses"] == 6
    sent = dict(backend.requests)[f"chain_update_search_{fx.CAS}"]["messages"][0]["content"]
    assert "[src6]" in sent and "src6 | Mise en service" in sent and url not in sent
    with ctx.db.connect() as conn:
        view = update.overview(conn, material_id)
        stored = conn.execute("SELECT url FROM sources WHERE material_id = ? AND ref = 'src6'",
                              (material_id,)).fetchone()
    assert stored[0] == url and view["items"][0]["source_refs"] == ["src6"]
    assert view["freshness"]["checked_at"] and view["freshness"]["age_days"] == 0
    assert not view["freshness"]["advised"]


def test_update_routes(client: TestClient, ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    output = example_update()
    output["changes"] = [{**output["changes"][0], "source_ids": ["src1"]}]
    backend = ReplayBackend(responses={f"chain_update_reread_{fx.CAS}": message(output)})
    ctx.llm_backend = lambda: backend
    ctx.http_client = lambda: mock_http()
    h = unsafe_headers(login(client, ctx))
    wrong = client.post(f"/api/materials/{material_id}/quote", json={}, headers=h)
    assert wrong.status_code == 409  # a validated chain is not reconstructed
    quote = client.post(f"/api/materials/{material_id}/update/quote", json={"kind": "reread"}, headers=h).json()
    assert quote["searches_max"] == 0 and quote["max_usd"] < 0.6
    other = client.post(f"/api/materials/{material_id}/update/quote", json={"kind": "search"}, headers=h).json()
    r = client.post(f"/api/materials/{material_id}/update", json={"kind": "reread", "quote_id": other["quote_id"]},
                    headers=h)
    assert r.status_code == 409  # a search quote does not pay for a reread
    job = client.post(f"/api/materials/{material_id}/update", json={"kind": "reread", "quote_id": quote["quote_id"]},
                      headers=h).json()
    ctx.jobs.shutdown(wait=True)
    assert client.get(f"/api/jobs/{job['job_id']}").json()["state"] == "done"
    chain = client.get(f"/api/materials/{material_id}/chain").json()
    item = chain["update"]["items"][0]
    assert chain["update"]["freshness"]["researched_at"] and item["decision"] == "pending"
    r = client.post(f"/api/materials/{material_id}/updates/{item['id']}", json={"accept": True}, headers=h)
    assert r.json() == {"ok": True}
    again = client.post(f"/api/materials/{material_id}/updates/{item['id']}", json={"accept": False}, headers=h)
    assert again.status_code == 409
    plant = next(n for n in client.get(f"/api/materials/{material_id}/chain").json()["nodes"]
                 if n["node_key"] == "plant_gulf_cracker")
    assert plant["status"] == "idled"
    r = client.patch(f"/api/materials/{material_id}/nodes/plant_gulf_cracker", json={"status": "teleported"},
                     headers=h)
    assert r.status_code == 422


# --- USGS MCS 2026 table: one figure per row, Windows-1252 ---------------------------------------------------------
LONG_CSV = (
    "MCS chapter,Section,Commodity,Country,Statistics,Statistics_detail,Unit,Year,Value,Notes\n"
    "LITHIUM,Salient Statistics—United States,Lithium,United States,Production,Production,metric tons,2025,W,\n"
    "LITHIUM,World Mine Production and Reserves,Lithium,Australia,Production,Mine production,metric tons,2024,80,\n"
    "LITHIUM,World Mine Production and Reserves,Lithium,Australia,Production,Mine production,metric tons,2025,90,\n"
    "LITHIUM,World Mine Production and Reserves,Lithium,Chile,Production,Mine production,metric tons,2025,60,\n"
    "LITHIUM,World Mine Production and Reserves,Lithium,Côte d’Ivoire,Production,Mine production,"
    "metric tons,2025,\"30\",\n"
    "LITHIUM,World Mine Production and Reserves,Lithium,United States,Production,Mine production,metric tons,2025,W,\n"
    "LITHIUM,World Mine Production and Reserves,Lithium,Other countries,Production,Mine production,metric tons,2025,"
    "—,\n"
    "LITHIUM,World Mine Production and Reserves,Lithium,World total,Production,Mine production: rounded,metric tons,"
    "2025,\"300\",\n"
    "LITHIUM,World Mine Production and Reserves,Lithium,Chile,Reserves,Reserves,metric tons,2025,\"9,000\",\n"
    "COPPER,World Mine and Refinery Production,Copper,Chile,Production,Mine production,thousand metric tons,2025,"
    "\"5,000\",\n"
    "COPPER,World Mine and Refinery Production,Copper,World total,Production,Mine production: rounded,"
    "thousand metric tons,2025,\"20,000\",\n"
    "COPPER,World Mine and Refinery Production,Copper,China,Production,Refinery production,thousand metric tons,"
    "2025,\"12,000\",\n"
    "COPPER,World Mine and Refinery Production,Copper,World total,Production,Refinery production: rounded,"
    "thousand metric tons,2025,\"25,000\",\n"
    "SLAG,World Production,Iron And Steel Slag,World total,Production,Slag,million tons,2025,190 - 290,\n"
)


def test_usgs_2026_table() -> None:
    ref = crosscheck.usgs_production_shares(LONG_CSV, "lithium", "https://www.usgs.gov/x")
    assert ref is not None and ref.basis == "production" and "mine production 2025" in ref.source
    assert ref.shares == {"AU": 30.0, "CL": 20.0, "CI": 10.0}  # W, — and reserves ignored; latest year only
    assert crosscheck.usgs_production_shares(LONG_CSV, "copper", "u") is None  # mine or refinery? say nothing
    mine = crosscheck.usgs_production_shares(LONG_CSV, "copper", "u", "mine")
    refinery = crosscheck.usgs_production_shares(LONG_CSV, "copper", "u", "refinery")
    assert mine and mine.shares == {"CL": 25.0} and refinery and refinery.shares == {"CN": 48.0}
    assert crosscheck._number("190 - 290") is None and crosscheck._number("10,000") == 10_000
    assert crosscheck.usgs_hint("Chine (cuivre raffiné)") == "refinery"
    assert crosscheck.usgs_hint("Chili (minerai de cuivre)") == "mine"
    assert crosscheck.usgs_hint("Chili (cuivre)") is None


def test_usgs_table_fetched_in_windows_1252() -> None:
    import httpx2

    body = LONG_CSV.encode("cp1252")
    from goulot.net.allowlist import Allowlist
    from goulot.net.client import build_client

    client = build_client(Allowlist(set()), transport=httpx2.MockTransport(lambda r: httpx2.Response(200, content=body)))
    text = crosscheck.fetch_usgs_csv(client, "https://www.sciencebase.gov/catalog/file/get/x")
    assert text is not None and "Côte d’Ivoire" in text
    chain = {"nodes": [
        {"node_id": "cl", "node_type": "country", "tier": 1, "country_iso2": "CL", "product": "Lithium",
         "name": "Chili", "global_share_pct": 74.5, "share_basis": "exports", "confidence": "medium",
         "lat": None, "lon": None},
        {"node_id": "au", "node_type": "country", "tier": 2, "country_iso2": "AU", "product": "Concentré de spodumène",
         "name": "Australie", "global_share_pct": 60.0, "share_basis": "production", "confidence": "medium",
         "lat": None, "lon": None}],
        "material": {"hs_code_candidates": []}}
    service.cross_check(service.Deps(None, None, None, None, usgs_csv=text), chain,  # type: ignore[arg-type]
                        {"name": "carbonate de lithium"})
    cl, au = chain["nodes"]
    assert cl["confidence"] == "medium" and cl["crosscheck"][0]["share_pct"] == 20.0  # exports: noted, not flagged
    assert au["confidence"] == "low" and "30.0 %" in au["anomalies"][0]


def test_usgs_address_is_configured() -> None:
    from goulot.collect.base import load_sources
    from goulot.net.allowlist import source_hosts
    from goulot.settings import CONFIG_DIR

    url = service.usgs_url()
    assert url.startswith("https://www.sciencebase.gov/catalog/file/get/") and load_sources()["usgs_mcs"]["enabled"]
    assert "www.sciencebase.gov" in source_hosts(CONFIG_DIR / "sources.yaml")


# --- the yearly USGS table, adopted by the app ---------------------------------------------------------------------
def scienceband(year: int, csv_body: bytes, *, title: str | None = None, name: str | None = None,
                status: int = 200) -> tuple[Any, list[str]]:
    """A fictitious ScienceBase: the search and the file it lists."""
    import httpx2

    from goulot.net.allowlist import Allowlist
    from goulot.net.client import build_client

    seen: list[str] = []
    file_url = "https://www.sciencebase.gov/catalog/file/get/abc?f=__disk__x"

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        if request.url.path == "/catalog/items":
            if status != 200:
                return httpx2.Response(status, json={})
            return httpx2.Response(200, json={"items": [
                {"title": f"Mineral Commodity Summaries {year - 1} Data Release - Commodity Salient",
                 "files": [{"name": f"MCS{year - 1}_Commodities_Data.csv", "url": file_url + "old"}]},
                {"title": title or f"Mineral Commodity Summaries {year} Data Release - Commodity Salient U.S. and World",
                 "files": [{"name": f"MCS{year}_Commodities_Data.xml", "url": file_url + "xml"},
                           {"name": name or f"MCS{year}_Commodities_Data.csv", "url": file_url}]}]})
        return httpx2.Response(200, content=csv_body)

    return build_client(Allowlist(set()), transport=httpx2.MockTransport(handler)), seen


def test_next_usgs_edition_is_adopted_once_it_reads(ctx: AppContext) -> None:
    from datetime import UTC, datetime, timedelta

    from goulot.chain import usgs_table

    feb = datetime(2027, 2, 10, tzinfo=UTC)
    with ctx.db.connect() as conn:
        shipped = usgs_table.state(conn)
        assert shipped["source"] == "config" and shipped["year"] == 2026
        assert not usgs_table.due(conn, datetime(2026, 11, 12, tzinfo=UTC))  # this year's table: nothing to do
        assert usgs_table.due(conn, feb)
    client, seen = scienceband(2027, LONG_CSV.encode("cp1252"))
    with ctx.db.transaction() as conn:
        assert usgs_table.check(conn, client, feb) == "adopted"
        now_used = usgs_table.state(conn)
        assert now_used["year"] == 2027 and now_used["source"] == "app" and now_used["url"].endswith("__disk__x")
        assert not usgs_table.due(conn, feb + timedelta(days=30))
        fine, text = usgs_table.doctor_line(conn, feb)
    assert service.usgs_url(ctx.db) == now_used["url"]  # the reconstruction reads the adopted table
    assert fine and "édition 2027 (adoptée automatiquement)" in text
    assert "q=Mineral+Commodity+Summaries+2027+Data+Release" in seen[0]


def test_usgs_edition_kept_when_missing_or_unreadable(ctx: AppContext) -> None:
    from datetime import UTC, datetime, timedelta

    from goulot.chain import usgs_table

    jan = datetime(2027, 1, 20, tzinfo=UTC)
    cases = [
        (scienceband(2027, b"", title="Mineral Commodity Summaries 2026 Data Release")[0], "not_published"),
        (scienceband(2027, b"", name="MCS2027_World_Data.csv")[0], "not_published"),
        (scienceband(2027, b"COMMODITY,COUNTRY\nLithium,Chile\n")[0], "unreadable"),
        (scienceband(2027, b"", status=503)[0], "failed"),
    ]
    for client, expected in cases:
        with ctx.db.transaction() as conn:
            assert usgs_table.check(conn, client, jan) == expected
            kept = usgs_table.state(conn)
            assert kept["year"] == 2026 and kept["source"] == "config" and kept["error"]
            assert not usgs_table.due(conn, jan + timedelta(days=3))  # once a week
            assert usgs_table.due(conn, jan + timedelta(days=7))
    with ctx.db.connect() as conn:
        assert usgs_table.doctor_line(conn, datetime(2027, 3, 1, tzinfo=UTC))[0]  # not late yet
        fine, text = usgs_table.doctor_line(conn, datetime(2027, 5, 2, tzinfo=UTC))
    assert not fine and "édition 2026" in text


def test_usgs_tick_runs_when_due_then_waits(ctx: AppContext) -> None:
    """The test clock starts in January 2027: the 2026 table in use is from an earlier year."""
    from goulot.chain import usgs_table

    opened: list[int] = []

    def http() -> Any:
        opened.append(1)
        return scienceband(2027, LONG_CSV.encode("cp1252"))[0]

    ctx.http_client = http  # type: ignore[method-assign]
    assert usgs_table.tick(ctx) == "adopted" and opened == [1]
    assert usgs_table.tick(ctx) is None and opened == [1]  # 2027 table in use now: nothing fetched
