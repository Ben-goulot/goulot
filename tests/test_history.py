"""Phase 3 (history): margin index, full history download, and their use on the material sheet."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import httpx2
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.ledger.rules import Series
from goulot.prices import collect, margin, nbs
from goulot.prices import store as prices
from tests.conftest import login
from tests.test_collect import validated_chain
from tests.test_nbs import page
from tests.test_prices import business_days, fill
from tests.test_prices import client as http_client


def test_margin_index_and_trend() -> None:
    days = business_days(date(2026, 1, 5), 30)
    # The material rises 10 % while its inputs stay flat: the margin widens.
    material = Series([(d, 100.0 + (10.0 if i >= 25 else 0.0)) for i, d in enumerate(days)], 1)
    inputs = Series([(d, 50.0) for d in days], 5)
    index = margin.margin_index(material, inputs)
    assert index is not None and index.points[0][1] == 100 and math.isclose(index.points[-1][1], 110.0)
    assert margin.trend(index) == ("up", 10.0)
    # Both rise together: a cost push, not a tension of the material.
    together = margin.margin_index(material, Series([(d, 50.0 * v / 100) for d, v in material.points], 5))
    assert together is not None and margin.trend(together) == ("flat", 0.0)
    assert margin.trend(Series(index.points[:10], 5)) is None  # fewer than 20 sessions


def test_margin_on_the_sheet_only_for_a_quoted_material(client: TestClient, ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    with ctx.db.transaction() as conn:
        a = prices.ensure_instrument(conn, "fred", "A", "Propylène", rank=1, frequency="daily")
        prices.link(conn, material_id, a, "input", proposed_by="user", validated=True, node_key="propylene")
        fill(conn, a, date(2026, 1, 5), [50.0] * 30)
    login(client, ctx)
    sheet = client.get(f"/api/materials/{material_id}/sheet").json()
    assert sheet["margin"] is None  # only an input index: no margin
    with ctx.db.transaction() as conn:
        q = prices.ensure_instrument(conn, "shfe_ine", "zz", "Cotation de test", rank=1, frequency="daily")
        prices.link(conn, material_id, q, "direct", proposed_by="user", validated=True)
        fill(conn, q, date(2026, 1, 5), [100.0] * 25 + [95.0] * 5)
    sheet = client.get(f"/api/materials/{material_id}/sheet").json()
    assert sheet["margin"]["trend"] == "down" and sheet["margin"]["change_pct_20"] == -5.0


def test_backfill_downloads_every_linked_series(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    with ctx.db.transaction() as conn:
        nbs.ensure_instruments(conn)
        prices.link(conn, material_id, "nbs_china_prices:benzene", "input", proposed_by="user", validated=True)
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "www.stats.gov.cn":
            return httpx2.Response(200, text=page("nbs_list_index") if request.url.path == "/sj/zxfb/" else
                                   page("nbs_2026-09-late"))
        return httpx2.Response(404)

    lines: list[str] = []
    written = collect.backfill(ctx.db, http_client(handler, seen), lambda name: None, material_id, lines.append)
    assert written[nbs.SOURCE] == 3 * 50
    assert any("Bureau national des statistiques" in line for line in lines)
    assert any("indisponible" in line for line in lines)  # the customs series answers 404 here, and is skipped


def test_material_without_series_gets_a_pressure_without_percentage(client: TestClient, ctx: AppContext) -> None:
    """Phase 3 criterion: without any price series, the pressure is computed from the events, never a percentage."""
    from goulot.llm.batch import ReplayBatchBackend
    from goulot.pipeline import assess
    from goulot.pipeline.assess import Allowance
    from tests.test_assess import analyses_by_custom_id, impact_for, suez_event
    from tests.test_collect import NOW

    event_id = suez_event(ctx)
    with ctx.db.connect() as conn:
        material_id = conn.execute("SELECT material_id FROM events WHERE id = ?", (event_id,)).fetchone()[0]
        assert prices.best_series(conn, material_id) is None
    backend = ReplayBatchBackend()
    assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, Allowance())
    backend.responses = analyses_by_custom_id(ctx, lambda eid: impact_for(eid, severity=3 if eid == event_id else 0))
    assess.collect(ctx.db, ctx.budget, backend)
    assess.refresh_verdicts(ctx.db, ctx.vault, NOW, verified=True, last_ok_cycle=NOW.isoformat())
    login(client, ctx)
    sheet = client.get(f"/api/materials/{material_id}/sheet").json()
    assert sheet["verdict"]["pressure"] > 0 and sheet["verdict"]["pressure_label"] in ("Hausse", "Forte hausse")
    assert sheet["series"] is None and sheet["margin"] is None
    assert all(e["impact"] is None for e in sheet["events"])  # no instrument: no hypothesis, no percentage


def test_materials_validated_before_price_links_get_them_once(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    with ctx.db.transaction() as conn:  # as in phase 2: validated without any price link
        conn.execute("DELETE FROM instrument_links WHERE material_id = ?", (material_id,))
    assert collect.catch_up_links(ctx.db) == 1
    with ctx.db.connect() as conn:
        links = prices.links(conn, material_id)
    assert {r["role"] for r in links} >= {"customs", "factor"}
    assert all(r["validated"] == 0 for r in links if r["role"] != "factor")  # proposed, to be validated
    assert collect.catch_up_links(ctx.db) == 0  # once only


def test_inputs_worded_differently_still_get_their_series(ctx: AppContext) -> None:
    from goulot.prices import czce, exchange_files, shfe

    assert czce.EXCHANGE.match_node("na2co3", "Carbonate de sodium (soude Solvay)") is not None
    assert czce.EXCHANGE.match_node("x", "propylène de qualité polymère") is not None
    assert nbs.match_node("h2so4", "Acide sulfurique 98 %") is nbs._BY_SLUG["sulfuric_acid"]
    assert shfe.EXCHANGE.match_node("x", "Délai fournisseur (lead time)") is None  # no English common words
    assert not exchange_files.mentions(("zinc",), "zincite")  # whole words only
    material_id = validated_chain(ctx)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE chain_nodes SET product = 'Carbonate de sodium (Na2CO3)' WHERE material_id = ? AND "
                     "node_key = 'ammonia'", (material_id,))
    lines: list[str] = []
    collect.backfill(ctx.db, http_client(lambda r: httpx2.Response(404)), lambda n: None, material_id, lines.append)
    assert any("Carbonate de soude (Zhengzhou)" in line and "intrant" in line for line in lines)
    lines.clear()
    collect.backfill(ctx.db, http_client(lambda r: httpx2.Response(404)), lambda n: None, material_id, lines.append)
    assert not any("Série proposée" in line for line in lines)  # proposed once


def test_history_inputs_command_shows_the_chain_type(ctx: AppContext, capsys: Any) -> None:
    from goulot.cli.main import _print_inputs

    validated_chain(ctx)
    with ctx.db.connect() as conn:
        material_id, name = conn.execute("SELECT id, name FROM materials").fetchone()
    _print_inputs(ctx.db, material_id, name)
    out = capsys.readouterr().out
    assert "rang 1 · material · propylene" in out and "Propylène (Zhengzhou)" in out
    assert "rang 1 · port · port_rotterdam" in out  # other boxes are listed too, without series
