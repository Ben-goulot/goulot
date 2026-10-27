"""Stop watching and resume (docs/SPEC.md, section 2): no query and no AI call for an archived material, its prices
kept while an advice is open, its rules back on resuming. Offline."""

from __future__ import annotations

from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.chain import lifecycle
from goulot.collect.runner import gdelt_queries, queries
from goulot.ledger import chain, outcomes
from goulot.pipeline import assess
from goulot.pipeline.assess import Allowance
from goulot.prices.collect import watched_instruments
from tests.conftest import login, unsafe_headers
from tests.test_screens_api import prepared


def test_archived_material_gets_no_query_and_no_ai_call(client: TestClient, ctx: AppContext) -> None:
    material_id, _ = prepared(ctx)
    with ctx.db.connect() as conn:
        rules_before = conn.execute("SELECT COUNT(*) FROM watch_rules WHERE material_id = ? AND active = 1",
                                    (material_id,)).fetchone()[0]
        assert queries(conn) and gdelt_queries(conn)
    csrf = login(client, ctx)
    assert client.post(f"/api/materials/{material_id}/archive", headers=unsafe_headers(csrf)).status_code == 200
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM watch_rules WHERE material_id = ? AND active = 1",
                            (material_id,)).fetchone()[0] == 0
        assert queries(conn) == [] and gdelt_queries(conn) == []
        assert assess.due_events(conn, Allowance()) == []  # its events wait: no analysis
    markets = client.get("/api/markets").json()
    assert markets["materials"] == [] and [m["id"] for m in markets["archived"]] == [material_id]
    # Twice archived, or resumed while watched: refused with a sentence.
    assert client.post(f"/api/materials/{material_id}/archive", headers=unsafe_headers(csrf)).status_code == 409
    r = client.post(f"/api/materials/{material_id}/resume", headers=unsafe_headers(csrf))
    assert r.status_code == 200 and r.json()["rules"] == rules_before  # the same chain, no reconstruction
    assert [m["id"] for m in client.get("/api/markets").json()["materials"]] == [material_id]


def test_prices_continue_while_an_advice_is_open(ctx: AppContext) -> None:
    material_id, _ = prepared(ctx)
    with ctx.db.transaction() as conn:
        conn.execute("INSERT INTO instruments (id, source, code, name, rank, frequency, created_at) "
                     "VALUES ('fred:TEST', 'fred', 'TEST', 'Série de test', 1, 'daily', '2026-10-18')")
        conn.execute("INSERT INTO instrument_links (material_id, instrument_id, role, proposed_by, validated, "
                     "created_at) VALUES (?, 'fred:TEST', 'direct', 'user', 1, '2026-10-18')", (material_id,))
        lifecycle.archive(conn, material_id)
        open_now = outcomes.open_advice(conn, material_id)
        watched = {r["id"] for r in watched_instruments(conn)}
    assert open_now > 0 and "fred:TEST" in watched  # the ledger must be able to judge its advices
    with ctx.db.transaction() as conn:
        for e in conn.execute("SELECT seq, kind FROM ledger WHERE material_id = ? AND kind IN "
                              "('verdict', 'hypothesis', 'pressure')", (material_id,)).fetchall():
            chain.append(conn, "outcome", {"of_seq": e["seq"], "of_kind": e["kind"], "result": "unresolved",
                                           "observed_pct": None, "on_index": False, "lead_weeks": None,
                                           "detail": "test", "series": "", "rank": None}, material_id=material_id)
        assert outcomes.open_advice(conn, material_id) == 0
        assert "fred:TEST" not in {r["id"] for r in watched_instruments(conn)}
