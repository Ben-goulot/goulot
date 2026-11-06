"""Phase 6, compliance export (docs/SPEC.md, section 10; CLAUDE.md, « Phase 6 »).

The three test CAS are rebuilt from their recorded answers (`tests/fixtures/llm/`), validated, then exported. The
recorded acrylonitrile chain gives no share by country: they are entered as a user correction, as the user would.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.chain import service, store
from goulot.cli.dev import FIXTURES, TEST_CAS
from goulot.compliance import concentration as conc
from goulot.compliance import report as compliance_report
from goulot.compliance import templates
from goulot.ledger import chain as ledger
from goulot.llm.backend import ReplayBackend
from tests.conftest import unsafe_headers

ACN, LI, GA = TEST_CAS
BANDS = {"low": 1500, "high": 2500}
NOW = datetime(2026, 11, 6, 10, tzinfo=UTC)
# Shares typed by the user on the Chaîne screen for the acrylonitrile plants (fictitious, by country)
ACN_SHARES = {"Chine (acrylonitrile)": 40.0, "INEOS Nitriles Green Lake": 15.0, "INEOS Nitriles Köln": 6.0,
              "AnQore Geleen (Chemelot)": 5.0}


# --- concentration on known shares -----------------------------------------------------------------------------
def test_hhi_on_known_shares() -> None:
    assert conc.hhi({"CN": 50, "AU": 30, "CL": 20}) == 3800
    assert conc.hhi({"A": 25, "B": 25, "C": 25, "D": 25}) == 2500
    assert conc.hhi({"CN": 100}) == 10000
    assert conc.hhi({}) is None
    f = conc.figures({"CN": 40, "US": 15, "KR": 10}, 65, BANDS)
    assert f["hhi"] == 1600 + 225 + 100 and f["hhi_band"] == "modérée"
    assert f["top_country"] == "CN" and f["top_share_pct"] == 40
    assert f["coverage_pct"] == 65 and f["lower_bound"] is True  # shares never scaled up to 100 %
    assert conc.figures({"A": 30, "B": 30, "C": 40}, 65, BANDS)["hhi_band"] == "forte"
    assert conc.figures({c: 10 for c in "ABCDEFGHIJ"}, 65, BANDS)["hhi_band"] == "faible"


@pytest.mark.parametrize(("shares", "country", "status"), [
    ({"CN": 98, "JP": 1, "DE": 1}, "CN", "dépassé"),
    ({"CN": 65, "CL": 20, "AR": 15}, "CN", "à la limite"),
    ({"CN": 64.9, "CL": 35.1}, "CN", "respecté"),
    ({"DE": 80, "US": 20}, "US", "respecté"),          # a member state of the Union is not a third country
    ({"FR": 70, "DE": 30}, None, "sans objet"),
])
def test_benchmark_65_on_third_countries(shares: dict[str, float], country: str | None, status: str) -> None:
    b = conc.benchmark(shares, 65)
    assert (b["country"], b["status"]) == (country, status)


def test_stages_of_the_recorded_gallium_chain() -> None:
    from tests.test_places import recorded_chain

    nodes = recorded_chain(GA)["nodes"]
    groups = {(g["stage"], g["tier"]): g for g in conc.stage_groups(nodes)}
    assert groups[("extraction", 3)]["shares"]["GN"] == 34  # bauxite, known by country only
    assert ("transformation", 2) in groups and groups[("transformation", 1)]["shares"] == {"CN": 99}
    assert any(stage == "recyclage" for stage, _ in groups)
    lithium = {(g["stage"], g["tier"]) for g in conc.stage_groups(recorded_chain(LI)["nodes"])}
    assert ("extraction", 2) in lithium and ("transformation", 1) in lithium  # a mine makes a tier « extraction »


def test_country_node_wins_over_its_sites() -> None:
    nodes = [{"tier": 1, "node_type": "country", "country_iso2": "CN", "global_share_pct": 60, "product": "x"},
             {"tier": 1, "node_type": "plant", "country_iso2": "CN", "global_share_pct": 20, "product": "x"},
             {"tier": 1, "node_type": "plant", "country_iso2": "US", "global_share_pct": 10, "product": "x"}]
    assert conc.national_shares(nodes) == {"CN": 60, "US": 10}


def test_templates_and_strategic_list() -> None:
    found = templates.available()
    assert set(found) == {"article24", "client_questionnaire"}
    for t in found.values():
        assert {s["id"] for s in t["sections"]} == {"mapping", "factors", "vulnerability", "mitigation", "method"}
        assert t["benchmarks"]["single_country_share_max_pct"] == 65
    assert templates.strategic_match(["Gallium"])["listed"]
    assert templates.strategic_match(["carbonate de lithium"])["entry"] == "Lithium, qualité batterie"
    assert not templates.strategic_match(["acrylonitrile", "propènenitrile"])["listed"]


# --- the three test CAS ------------------------------------------------------------------------------------------
def rebuild(ctx: AppContext, cas: str) -> str:
    directory = FIXTURES / cas
    identity = json.loads((directory / "identity.json").read_text(encoding="utf-8"))
    with ctx.db.transaction() as conn:
        material_id = store.create_material(conn, identity)
    job_id = service.start_job(ctx.db, material_id)
    service.reconstruct(service.Deps(ctx.db, ctx.budget, ReplayBackend(directory), None), material_id, job_id,
                        budget_name="dev")
    with ctx.db.transaction() as conn:
        if cas == ACN:
            for n in store.load_chain(conn, material_id)["nodes"]:
                if n["name"] in ACN_SHARES:
                    store.correct_node(conn, material_id, n["node_key"], {"global_share_pct": ACN_SHARES[n["name"]]})
        store.validate_material(conn, material_id, acknowledge_injection=True)
    return material_id


@pytest.fixture
def three(ctx: AppContext) -> dict[str, str]:
    return {cas: rebuild(ctx, cas) for cas in TEST_CAS}


def body(ids: list[str], **changes: Any) -> dict[str, Any]:
    return {"template": "article24", "material_ids": ids, "basis": "typical", "company": "Entreprise fictive",
            "signatory_name": "Camille Exemple", "signatory_role": "Responsable des achats", "reviewed": True,
            **changes}


def choose_measures(client: TestClient, headers: dict[str, str], ids: list[str]) -> None:
    profile = {"company": "Entreprise fictive", "signatory_name": "Camille Exemple",
               "signatory_role": "Responsable des achats",
               "measures": {mid: {"choices": ["stock_de_securite", "second_fournisseur"]} for mid in ids}}
    r = client.put("/api/private/compliance/profile", json=profile, headers=headers)
    assert r.status_code == 200, r.text


def test_complete_report_for_the_three_test_cas(client: TestClient, ctx: AppContext, opened: tuple[str, str],
                                                three: dict[str, str]) -> None:
    csrf, token = opened
    headers = unsafe_headers(csrf, token)
    ids = [three[c] for c in TEST_CAS]
    choose_measures(client, headers, ids)
    for template in ("article24", "client_questionnaire"):
        r = client.post("/api/private/compliance/export", json=body(ids, template=template), headers=headers)
        assert r.status_code == 200, r.text
        out = r.json()
        report = out["report"]
        failed = [c for c in report["checks"] if not c["ok"]]
        assert not failed, failed
        assert report["draft"] is False and report["draft_label"] is None
        assert report["confidential"] is False
        # the fingerprint is the SHA-256 of the JSON annex without its own fields
        annex = json.loads(out["files"]["json"]["content"])
        assert annex["fingerprint"] == compliance_report.fingerprint(annex) == report["fingerprint"]
        assert annex["ledger_seq"] == report["ledger_seq"]
        rows = list(csv.reader(io.StringIO(out["files"]["csv"]["content"].lstrip("﻿")), delimiter=";"))
        assert rows[0] == compliance_report.CSV_COLUMNS and rows[-1][8] == report["fingerprint"]
        with ctx.db.connect() as conn:
            entry = ledger.entries(conn, kinds=("export",))[-1]
            assert entry["seq"] == report["ledger_seq"] and entry["fingerprint"] == report["fingerprint"]
            assert entry["materials"] == ids and entry["draft"] is False
            kept = conn.execute("SELECT * FROM compliance_exports WHERE ledger_seq = ?", (entry["seq"],)).fetchone()
            assert kept["fingerprint"] == report["fingerprint"] and kept["basis"] == "typical"
            assert ledger.verify(conn).ok
    by_cas = {m["cas"]: m for m in report["materials"]}
    ga = by_cas[GA]
    assert ga["strategic"]["listed"] and not by_cas[ACN]["strategic"]["listed"]
    assert ga["most_concentrated"]["top_country"] == "CN" and ga["most_concentrated"]["benchmark"]["status"] == "dépassé"
    assert ga["most_concentrated"]["hhi"] == 9801
    acn = by_cas[ACN]
    assert not next(s for s in acn["mapping"] if s["stage"] == "extraction")["documented"]  # flagged as such
    assert acn["concentration"][0]["top_share_pct"] == 40 and acn["concentration"][0]["hhi"] == 1600 + 225 + 36 + 25
    assert all(m["vulnerability"]["propagation"] for m in report["materials"])
    assert all(m["mitigation"]["choices"] for m in report["materials"])


def test_incomplete_report_is_a_draft(client: TestClient, ctx: AppContext, three: dict[str, str]) -> None:
    from tests.conftest import login

    csrf = login(client, ctx)
    ids = [three[GA]]
    # vault closed: the mitigation choices stay in the vault, so the report is a draft
    r = client.post("/api/compliance/preview", json=body(ids), headers=unsafe_headers(csrf))
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["draft"] is True and report["draft_label"] == "Brouillon : rapport incomplet"
    assert next(c for c in report["checks"] if c["id"] == "mitigation")["ok"] is False
    assert report["materials"][0]["vulnerability"]["company_coverage"] == {"status": "coffre fermé"}
    # no signatory, not reviewed
    r = client.post("/api/compliance/preview", json=body(ids, signatory_name="", reviewed=False),
                    headers=unsafe_headers(csrf))
    assert not next(c for c in r.json()["checks"] if c["id"] == "validation")["ok"]
    # an export of a draft is still recorded, as a draft
    r = client.post("/api/compliance/export", json=body(ids), headers=unsafe_headers(csrf))
    assert r.status_code == 200 and r.json()["report"]["draft"] is True
    with ctx.db.connect() as conn:
        assert ledger.entries(conn, kinds=("export",))[-1]["draft"] is True


def test_old_chain_makes_a_draft(ctx: AppContext, three: dict[str, str]) -> None:
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE materials SET validated_at = '2025-09-01T00:00:00+00:00' WHERE id = ?", (three[LI],))
    req = compliance_report.Request(template=templates.get("article24"), material_ids=[three[LI]],
                                    signatory_name="A", signatory_role="B", reviewed=True)
    private = compliance_report.PrivateInputs(measures={three[LI]: {"choices": ["contrat_cadre"]}})
    with ctx.db.connect() as conn:
        report = compliance_report.build(conn, req, NOW, private)
    assert report["draft"] and not next(c for c in report["checks"] if c["id"] == "chain_date")["ok"]
    private = compliance_report.PrivateInputs(measures={three[LI]: {"choices": ["autre"], "comment": " "}})
    with ctx.db.connect() as conn:
        report = compliance_report.build(conn, req, NOW, private)
    assert report["materials"][0]["checks"]["mitigation"]["detail"] == "« Autre » demande un commentaire"


def test_cli_export_is_a_typical_chain_draft(ctx: AppContext, three: dict[str, str], tmp_path: Any,
                                             capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
                                             ) -> None:
    from goulot.cli import main as cli

    monkeypatch.setattr(cli, "load_settings", lambda: ctx.settings)
    monkeypatch.setattr("goulot.cli.updates.is_workcopy", lambda: False)
    out = tmp_path / "out"
    out.mkdir()
    cli.main(["export", "--template", "article24", "--cas", GA, LI, "--out", str(out), "--signataire", "A",
              "--fonction", "B"])
    files = sorted(p.name for p in out.iterdir())
    assert len(files) == 2 and files[0].endswith(".csv") and files[1].endswith(".json")
    report = json.loads((out / files[1]).read_text(encoding="utf-8"))
    assert report["draft"] is True and report["basis"] == "typical"
    assert [m["cas"] for m in report["materials"]] == [GA, LI]
    assert "Brouillon" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["export", "--cas", "0000-00-0", "--out", str(out)])
