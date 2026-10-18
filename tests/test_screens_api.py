"""Phase 3, lot 6: API of « Marchés », « Fiche matière » and « Fiabilité »."""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.llm.batch import ReplayBatchBackend
from goulot.pipeline import assess
from goulot.prices import store as prices
from tests.conftest import login, unsafe_headers
from tests.test_assess import analyses_by_custom_id, impact_for, suez_event
from tests.test_collect import NOW
from tests.test_prices import fill


def prepared(ctx: AppContext) -> tuple[str, str]:
    ctx.clock._wall = NOW.timestamp()  # type: ignore[attr-defined]  # the screens look at the last 60 days
    event_id = suez_event(ctx)
    backend = ReplayBatchBackend()
    assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, assess.Allowance())
    backend.responses = analyses_by_custom_id(ctx, lambda eid: impact_for(eid, severity=2 if eid == event_id else 0))
    assess.collect(ctx.db, ctx.budget, backend)
    assess.refresh_verdicts(ctx.db, ctx.vault, NOW, verified=True, last_ok_cycle=NOW.isoformat())
    with ctx.db.connect() as conn:
        material_id = conn.execute("SELECT material_id FROM events WHERE id = ?", (event_id,)).fetchone()[0]
    return material_id, event_id


def test_markets_and_sheet(client: TestClient, ctx: AppContext) -> None:
    material_id, event_id = prepared(ctx)
    login(client, ctx)
    markets = client.get("/api/markets").json()
    (m,) = markets["materials"]
    assert m["verdict"]["label"] == "Problème potentiel à 6 sem." and m["verdict"]["break_in"] == 1
    assert m["verdict"]["pressure_label"] == "Hausse" and "freshness" in markets
    sheet = client.get(f"/api/materials/{material_id}/sheet").json()
    events = {e["id"]: e for e in sheet["events"]}
    suez = events[event_id]
    assert suez["status_label"] == "En cours" and suez["letter"] in "ABCDEFGH"
    assert suez["lat"] is not None and "Canal de Suez" in suez["nodes"]
    assert sheet["analysis"]["headline"] and isinstance(sheet["analysis"]["triggers"], list)
    assert {link["role"] for link in sheet["links"]} >= {"customs", "factor"}
    assert any(s["type"] == "port" for s in sheet["sites"])
    assert any(u["source"] == "chaîne" for u in sheet["upcoming"])
    assert client.get("/api/materials/not-a-uuid/sheet").status_code == 404


def test_series_links_added_and_validated(client: TestClient, ctx: AppContext) -> None:
    material_id, _ = prepared(ctx)
    h = unsafe_headers(login(client, ctx))
    r = client.post(f"/api/materials/{material_id}/links",
                    json={"source": "yahoo_chart", "code": "ACN=F", "role": "direct", "name": "Acrylonitrile (test)"},
                    headers=h)
    assert r.status_code == 200 and r.json()["instrument_id"] == "yahoo_chart:ACN=F"
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT rank FROM instruments WHERE id = 'yahoo_chart:ACN=F'").fetchone()[0] == 6
    bad = client.post(f"/api/materials/{material_id}/links", json={"source": "evil", "code": "x", "role": "direct",
                                                                  "name": "x"}, headers=h)
    assert bad.status_code == 422
    customs = "us_census_trade:292610"
    assert client.post(f"/api/materials/{material_id}/links/{customs}/validate", json={}, headers=h).json() == {"ok": True}
    sheet = client.get(f"/api/materials/{material_id}/sheet").json()
    assert next(link for link in sheet["links"] if link["instrument_id"] == customs)["validated"] is True


def test_reliability_screen(client: TestClient, ctx: AppContext) -> None:
    material_id, _ = prepared(ctx)
    with ctx.db.transaction() as conn:
        a = prices.ensure_instrument(conn, "fred", "ACN", "Acrylonitrile (test)", rank=1, frequency="daily")
        prices.link(conn, material_id, a, "direct", proposed_by="user", validated=True)
        fill(conn, a, date(2026, 3, 2), [1400.0] * 150)
    login(client, ctx)
    data = client.get(f"/api/reliability?material_id={material_id}").json()
    assert data["ledger"]["ok"] is True and data["ledger"]["entries"] >= 1
    assert data["indicators"]["break_in"] is True
    assert any(a["kind"] == "verdict" and a["verdict"] == "pot" for a in data["advices"])
    assert data["series"]["name"] == "Acrylonitrile (test)" and data["series"]["rank"] == 1
    assert client.get("/api/reliability").json()["ledger"]["ok"] is True
