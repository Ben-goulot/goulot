"""Phase 3 (history): library of past cases, measured reactions, similarity, and their use in the analysis."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.history import leads, library, reactions, similarity
from goulot.llm.backend import ReplayBackend
from goulot.prices import store as prices
from tests import chain_fixtures as fx
from tests.conftest import login, unsafe_headers
from tests.test_collect import validated_chain
from tests.test_prices import client as http_client

FIXTURES = Path(__file__).parent / "fixtures"
HISTORY = json.loads((FIXTURES / "history" / "reactions.json").read_text(encoding="utf-8"))


def insert_case(conn: Any, material_id: str, case_id: str, day: str, status: str = "verified", **extra: Any) -> int:
    row = {"title": f"Cas {case_id}", "event_type": "industrial_outage", "end_date": "unknown", "place": "Texas",
           "country_iso2": "US", "node_type": "plant", "affected": '["acrylonitrile"]', "capacity_share_pct": 3.0,
           "capacity_share_basis": "capacité mondiale", "direction": "tightening", "duration_days": 30.0,
           "summary": "Résumé.", "confidence": "high", **extra}
    cur = conn.execute(
        "INSERT INTO historical_events (material_id, case_id, title, event_type, first_report_date, end_date, place, "
        "country_iso2, node_type, affected, capacity_share_pct, capacity_share_basis, direction, duration_days, "
        "summary, confidence, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "'2026-10-19')",
        (material_id, case_id, row["title"], row["event_type"], day, row["end_date"], row["place"],
         row["country_iso2"], row["node_type"], row["affected"], row["capacity_share_pct"],
         row["capacity_share_basis"], row["direction"], row["duration_days"], row["summary"], row["confidence"],
         status))
    conn.execute("INSERT INTO case_sources (case_row, ref, url, title, publisher, reliability) VALUES "
                 "(?, 'src1', 'https://www.reuters.com/x', 'x', 'reuters.com', 0.7)", (cur.lastrowid,))
    return int(cur.lastrowid)


def history_material(ctx: AppContext) -> tuple[str, dict[str, int]]:
    """The test chain (acrylonitrile, organic: Brent factor) with the synthetic series and cases of the fixture."""
    material_id = validated_chain(ctx)
    rows: dict[str, int] = {}
    with ctx.db.transaction() as conn:
        quote = prices.ensure_instrument(conn, "fred", "MAT", "Cotation synthétique", rank=1, frequency="daily")
        prices.link(conn, material_id, quote, "direct", proposed_by="user", validated=True)
        prices.upsert(conn, quote, [(date.fromisoformat(d), v) for d, v in HISTORY["material_series"]])
        prices.upsert(conn, reactions.BRENT, [(date.fromisoformat(d), v) for d, v in HISTORY["brent_series"]])
        for c in HISTORY["cases"]:
            rows[c["key"]] = insert_case(conn, material_id, f"case_{c['key'].lower()}", c["first_report_date"],
                                         c["status"])
    return material_id, rows


# --- reactions (tests/fixtures/history) -------------------------------------------------------------------------
def test_own_changes_medians_and_exclusions(ctx: AppContext) -> None:
    material_id, rows = history_material(ctx)
    with ctx.db.connect() as conn:
        found = library.cases(conn, material_id, statuses=("draft", "verified", "rejected"))
        measured = reactions.for_cases(conn, material_id, found)
    by_key = {k: next(r for r in measured[row] if r.instrument_id == "fred:MAT") for k, row in rows.items()}
    for key in ("C", "E"):  # clean β windows: the known β of 2 is found back
        r = by_key[key]
        assert r.beta == pytest.approx(HISTORY["beta"], abs=0.01) and r.factor == reactions.BRENT
        for h, want in HISTORY["expected"][key].items():
            assert r.raw[int(h)] == pytest.approx(want["raw"], abs=0.02)
            assert r.own[int(h)] == pytest.approx(want["own"], abs=0.05)
    s = reactions.summarize(found, measured, "fred:MAT", 20)
    want = HISTORY["expected_median_20"]
    assert (s.n, s.excluded_overlap) == (want["n"], want["excluded_overlap"])  # A and B overlap; D is a draft
    assert s.median == pytest.approx(want["median"], abs=0.05)
    assert (s.low, s.high) == (pytest.approx(want["low"], abs=0.05), pytest.approx(want["high"], abs=0.05))
    assert reactions.overlapping({c["id"]: c["day"] for c in found if c["status"] != "rejected"}) == {
        rows["A"], rows["B"]}


def test_no_factor_for_mineral_materials_and_slow_series() -> None:
    assert reactions.factor_for("C3H3N") == reactions.BRENT
    assert reactions.factor_for("CLi2O3") is None  # lithium carbonate: carbon, no hydrogen
    assert reactions.factor_for("Ga") is None and reactions.factor_for("") is None
    monthly = reactions.horizons("customs")
    assert monthly == (1, 2, 3) and reactions.horizons("tenday") == (1, 2, 6)


def test_replay_uses_only_prices_known_then(ctx: AppContext) -> None:
    material_id, rows = history_material(ctx)
    with ctx.db.connect() as conn:
        found = [c for c in library.cases(conn, material_id) if c["id"] == rows["E"]]
        early = reactions.for_cases(conn, material_id, found, as_of=date(2027, 3, 10))[rows["E"]]
    r = next(r for r in early if r.instrument_id == "fred:MAT")
    assert r.raw[1] is not None and r.raw[5] is not None and r.raw[20] is None and r.raw[60] is None


# --- preparation: checks by the code ----------------------------------------------------------------------------
def material() -> library.CaseMaterial:
    return library.CaseMaterial("acrylonitrile", "107-13-1", ["propylène", "ammoniac"], ["US", "CN"],
                                ["Usine A"], date(2016, 1, 1))


def raw_case(**changes: Any) -> dict[str, Any]:
    case = json.loads((Path("schemas/examples/historical_case_v1.example.json")).read_text(encoding="utf-8"))["cases"][0]
    return {**case, "affected_commodities": ["Acrylonitrile"], "source_ids": ["src1"], **changes}


SOURCES = [{"ref": "src1", "url": "https://www.reuters.com/a", "title": "A"},
           {"ref": "src2", "url": "https://random-blog.example/b", "title": "B"}]


def test_case_checks() -> None:
    result = {"cases": [
        raw_case(case_id="2021 Texas Tempête!", first_report_date="2021-02-15"),
        raw_case(case_id="2021 Texas Tempête!", first_report_date="2021-02-16", capacity_share_pct=140),
        raw_case(case_id="futur", first_report_date="2030-01-01"),
        raw_case(case_id="autre", affected_commodities=["unobtainium"]),
        raw_case(case_id="blog", source_ids=["src2", "src9"]),
        raw_case(case_id="sans_source", source_ids=["src9"]),
        raw_case(case_id="casse", event_type="INDUSTRIAL_OUTAGE", direction="Tightening"),
    ]}
    checked = {c.data["case_id"]: c for c in library.check_cases(result, material(), SOURCES, date(2026, 10, 19))}
    assert set(checked) == {"2021_texas_temp_te", "2021_texas_temp_te_2", "blog", "casse"}
    assert checked["2021_texas_temp_te_2"].data["capacity_share_pct"] is None
    assert checked["blog"].data["source_ids"] == ["src2"] and any("non vérifiable" in a for a in
                                                                    checked["blog"].anomalies)
    assert checked["casse"].data["event_type"] == "industrial_outage" and checked["casse"].data["direction"] == (
        "tightening")
    assert checked["casse"].data["affected_commodities"] == ["acrylonitrile"]


def research_response() -> dict[str, Any]:
    msg = fx.research_response()
    msg["content"].append({"type": "text", "text": "\nTempête de février 2021 au Texas [lead1].", "citations": []})
    return msg


def structure_response(cases: list[dict[str, Any]], *, cas: str = "107-13-1", injection: bool = False
                       ) -> dict[str, Any]:
    body = {"schema_version": "1.0", "material_cas": cas, "cases": cases, "open_questions": [],
            "injection_suspected": injection}
    return fx.structure_response(text=json.dumps(body))


def gdacs_http() -> httpx2.Client:
    body = (FIXTURES / "sources" / "gdacs_search_tc_2021_us.json").read_text(encoding="utf-8")

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "www.gdacs.org" and request.url.params.get("fromDate") == "2021-01-01":
            return httpx2.Response(200, text=body)
        return httpx2.Response(204)

    return http_client(handler)


def test_gdacs_leads_touch_the_chain_countries() -> None:
    found = leads.gdacs(gdacs_http(), {"US"}, date(2020, 1, 1), date(2022, 6, 1))
    assert [(lead.ref, lead.day.isoformat(), lead.countries) for lead in found] == [
        ("lead1", "2021-06-30", ("US",)), ("lead2", "2021-08-26", ("US",))]
    assert found[1].url == "https://www.gdacs.org/report.aspx?eventid=1000818&eventtype=TC"
    assert leads.gdacs(gdacs_http(), {"FR"}, date(2020, 1, 1), date(2022, 6, 1)) == []


def test_prepare_stores_drafts_with_their_sources(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    cases = [raw_case(case_id="tempete", first_report_date="2021-06-30", source_ids=["src1", "lead1"]),
             raw_case(case_id="blog", source_ids=["src9"])]
    backend = ReplayBackend(responses={"case_research_107-13-1": research_response(),
                                       "case_structure_107-13-1": structure_response(cases)})
    job_id = library.start_job(ctx.db, material_id)
    cost = library.prepare(library.CaseDeps(ctx.db, ctx.budget, backend, gdacs_http()), material_id, job_id,
                           today=date(2026, 10, 19))
    assert cost > 0
    (_, params), (_, structure_params) = backend.requests
    assert "<leads>" in params["messages"][0]["content"] and "IDA-21" in params["messages"][0]["content"]
    assert params["tools"][0]["max_uses"] == library.MAX_SEARCHES
    assert "https://" not in structure_params["messages"][0]["content"]  # the structuring call never sees a URL
    assert "lead1 |" in structure_params["messages"][0]["content"]  # the cited lead joins the sources
    with ctx.db.connect() as conn:
        stored = library.cases(conn, material_id)
        job = conn.execute("SELECT state, message FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job["state"] == "done" and "1 cas préparés" in job["message"]
    (case,) = stored
    assert case["status"] == "draft" and {s["ref"] for s in case["sources"]} == {"src1", "lead1"}
    assert next(s for s in case["sources"] if s["ref"] == "lead1")["url"].startswith("https://www.gdacs.org/")


def test_prepare_refuses_another_material_or_bad_output(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    for structure in (structure_response([raw_case()], cas="554-13-2"), fx.structure_response(text="{")):
        backend = ReplayBackend(responses={"case_research_107-13-1": research_response(),
                                           "case_structure_107-13-1": structure})
        job_id = library.start_job(ctx.db, material_id)
        library.prepare(library.CaseDeps(ctx.db, ctx.budget, backend, None), material_id, job_id)
        with ctx.db.connect() as conn:
            assert conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == "failed"
            assert conn.execute("SELECT COUNT(*) FROM historical_events").fetchone()[0] == 0


# --- review, similarity, dossier --------------------------------------------------------------------------------
def test_unreliable_case_cannot_be_verified(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    with ctx.db.transaction() as conn:
        row = insert_case(conn, material_id, "blog", "2021-02-15", "draft")
        conn.execute("UPDATE historical_events SET anomalies = ? WHERE id = ?",
                     (json.dumps(["Aucune source de fiabilité suffisante (0,7) : cas non vérifiable."]), row))
        with pytest.raises(library.ReviewError):
            library.review(conn, material_id, row, "verify")
        library.review(conn, material_id, row, "reject")


def event_target(**changes: Any) -> similarity.Target:
    base = {"event_type": "industrial_outage", "node_type": "plant", "share_pct": 3.0, "country_iso2": "US",
            "day": date(2027, 6, 1)}
    return similarity.Target(**{**base, **changes})


def test_similarity_threshold_and_drafts(ctx: AppContext) -> None:
    material_id, rows = history_material(ctx)
    with ctx.db.connect() as conn:
        found = similarity.analogues(conn, material_id, event_target())
        other = similarity.analogues(conn, material_id, event_target(event_type="cyber_incident",
                                                                      node_type="port", share_pct=None))
    keys = {v: k for k, v in rows.items()}
    assert {keys[a.case["id"]] for a in found} == {"C", "E"}  # verified, not overlapping; never D (draft) nor R
    assert all(a.points >= similarity.THRESHOLD for a in found) and other == []
    assert similarity.summary_line(found) == "Pas assez de cas comparables."
    with ctx.db.connect() as conn:  # replay: before E was reported, only C
        early = similarity.analogues(conn, material_id, event_target(day=date(2026, 1, 5)))
    assert [keys[a.case["id"]] for a in early] == ["C"]


def test_summary_line_with_enough_cases() -> None:
    def analogue(year: int, own: float) -> similarity.Analogue:
        r = reactions.Reaction("x", "x", "daily", date(year, 1, 1), {20: own}, {20: own}, None, None)
        return similarity.Analogue({"day": date(year, 1, 2)}, 8, [r])

    line = similarity.summary_line([analogue(2017, 4.0), analogue(2021, 10.0), analogue(2022, -2.0)])
    assert line.startswith("3 cas comparables (2017, 2021, 2022) : variation propre médiane de +4,0 % à 20 séances")
    assert "entre -0,8 et +8,8 % (de 10 % à 90 % des cas" in line and "pas causée par" in line


def test_dossier_receives_verified_analogues_only(ctx: AppContext) -> None:
    from goulot.llm.batch import ReplayBatchBackend
    from goulot.pipeline import assess, grouping
    from goulot.pipeline.assess import Allowance
    from tests.test_collect import NOW
    from tests.test_triage_events import collect_cycle

    history_material(ctx)
    collect_cycle(ctx)
    with ctx.db.transaction() as conn:
        grouping.events_from_measurements(conn)
        suez = conn.execute("SELECT * FROM events WHERE title LIKE '%Suez%'").fetchone()
        # Past closures of the same strait, in the same region: comparable to the current one.
        conn.execute("UPDATE historical_events SET event_type = 'maritime_disruption', node_type = 'chokepoint', "
                     "country_iso2 = 'EG', capacity_share_pct = NULL")
    backend = ReplayBatchBackend()
    assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, Allowance())
    (requests,) = backend.batches.values()
    text = next(p for _, p in requests if suez["id"] in p["messages"][0]["content"])["messages"][0]["content"]
    block = text.split("<analogues>")[1].split("</analogues>")[0]
    assert '"A1"' in block and '"A2"' not in block and "Cas case_c" in block and "fred:MAT" in block
    assert "case_d" not in block and "case_r" not in block  # never a draft nor a rejected case
    assert "case_e" not in block  # reported after the event (replay rule)
    assert "case_a" not in block and "case_b" not in block  # overlapping cases are left out


# --- routes ------------------------------------------------------------------------------------------------------
def test_routes_quote_kind_and_review(client: TestClient, ctx: AppContext) -> None:
    material_id, rows = history_material(ctx)
    csrf = login(client, ctx)
    listing = client.get(f"/api/materials/{material_id}/cases").json()
    assert {c["status"] for c in listing["cases"]} == {"verified", "draft", "rejected"}
    summary = next(s for s in listing["summaries"] if s["instrument_id"] == "fred:MAT")
    assert summary["n"] == 2 and summary["excluded_overlap"] == 2
    # A reconstruction quote cannot launch a case preparation.
    other = client.post(f"/api/materials/{material_id}/quote", headers=unsafe_headers(csrf))
    assert other.status_code == 409  # the chain is validated: no reconstruction quote anyway
    with ctx.db.transaction() as conn:
        from datetime import UTC, datetime

        from goulot.chain.quote import reconstruction_quote, save_quote

        foreign = save_quote(conn, material_id, reconstruction_quote(), datetime.fromtimestamp(ctx.clock.time(), UTC))
    refused = client.post(f"/api/materials/{material_id}/cases/prepare", json={"quote_id": foreign},
                          headers=unsafe_headers(csrf))
    assert refused.status_code == 409
    quote = client.post(f"/api/materials/{material_id}/cases/quote", headers=unsafe_headers(csrf)).json()
    assert 0 < quote["estimate_usd"] < quote["max_usd"] < 3  # criterion: under 3 $ per material
    ok = client.post(f"/api/materials/{material_id}/cases/{rows['D']}/review", json={"action": "verify"},
                     headers=unsafe_headers(csrf))
    assert ok.status_code == 200
    assert client.post(f"/api/materials/{material_id}/cases/{rows['D']}/review", json={"action": "drop"},
                       headers=unsafe_headers(csrf)).status_code == 422
    assert client.get("/api/events/EVT-2026-10-18-9999/analogues").status_code == 404
    assert client.get("/api/events/not-an-event/analogues").status_code == 404


def test_card_shows_excluded_cases_without_counting_them(client: TestClient, ctx: AppContext) -> None:
    from goulot.pipeline import grouping
    from tests.test_triage_events import collect_cycle

    history_material(ctx)
    collect_cycle(ctx)
    with ctx.db.transaction() as conn:
        grouping.events_from_measurements(conn)
        suez = conn.execute("SELECT id FROM events WHERE title LIKE '%Suez%'").fetchone()[0]
        conn.execute("UPDATE historical_events SET event_type = 'maritime_disruption', node_type = 'chokepoint', "
                     "country_iso2 = 'EG', capacity_share_pct = NULL")
    login(client, ctx)
    card = client.get(f"/api/events/{suez}/analogues").json()
    states = {r["case_id"]: r["state"] for r in card["rows"]}
    # Kept: C (verified). Shown, not counted: A and B (overlap), D (draft). Never: E (later), R (rejected).
    assert states == {"case_c": "kept", "case_a": "overlap", "case_b": "overlap", "case_d": "draft"}
    assert card["band"] is None and card["summary"] == "Pas assez de cas comparables."  # 1 case kept
    assert card["instrument_id"] == "fred:MAT"
    assert next(r for r in card["rows"] if r["case_id"] == "case_c")["own_20"] == pytest.approx(
        HISTORY["expected"]["C"]["20"]["own"], abs=0.05)


RECORDED_CASES = {"107-13-1": 9, "554-13-2": 16, "7440-55-3": 7}


def test_recorded_cases_meet_phase_3_criteria(ctx: AppContext) -> None:
    """Replays the case preparations recorded by `goulot dev record-cases` (none before the user runs it)."""
    from goulot.chain import service, store
    from goulot.cli.dev import CASES_DIR, FIXTURES, MIN_CASES, TEST_CAS

    recorded = [cas for cas in TEST_CAS if (FIXTURES / cas / CASES_DIR / "summary.json").is_file()]
    assert recorded == list(TEST_CAS)  # all three are recorded since 2026-10-19
    for cas in TEST_CAS:  # nothing half-recorded: a research without its summary would replay nothing
        out = FIXTURES / cas / CASES_DIR
        assert cas in recorded or not (out / f"case_structure_{cas}.json").is_file(), cas
    for cas in recorded:
        out = FIXTURES / cas / CASES_DIR
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        identity = json.loads((FIXTURES / cas / "identity.json").read_text(encoding="utf-8"))
        with ctx.db.transaction() as conn:
            material_id = store.create_material(conn, identity)
        service.reconstruct(service.Deps(ctx.db, ctx.budget, ReplayBackend(FIXTURES / cas), None), material_id,
                            service.start_job(ctx.db, material_id), budget_name="dev")
        found_leads = [leads.Lead.from_dict(d) for d in json.loads((out / "leads.json").read_text("utf-8"))]
        job_id = library.start_job(ctx.db, material_id)
        library.prepare(library.CaseDeps(ctx.db, ctx.budget, ReplayBackend(out), None, found_leads), material_id,
                        job_id, budget_name="dev", today=date.fromisoformat(summary["today"]))
        with ctx.db.connect() as conn:
            found = library.cases(conn, material_id)
        assert len(found) >= MIN_CASES and len(found) == summary["cases"], cas
        assert len(found) >= RECORDED_CASES[cas], cas  # the numbers accepted on 2026-10-19 (docs/DECISIONS.md)
        assert summary["cost_usd"] < 3, cas
        assert all(c["status"] == "draft" and c["sources"] for c in found)  # a draft until the user verifies it
        assert all(s["url"].startswith("https://") for c in found for s in c["sources"])
        verifiable = [c for c in found if not any("non vérifiable" in a for a in c["anomalies"])]
        assert all(max(s["reliability"] for s in c["sources"]) >= library.MIN_RELIABILITY for c in verifiable)
