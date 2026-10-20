"""Phase 3, lot 5: price readers, storage, best series, proxy index, statistics and their use downstream."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from typing import Any

import httpx2

from goulot.api.context import AppContext
from goulot.ledger import chain
from goulot.ledger.rules import Series
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from goulot.pipeline import assess
from goulot.prices import collect, sources, stats
from goulot.prices import store as prices
from tests.test_collect import NOW, validated_chain


def client(handler: Any, seen: list[httpx2.Request] | None = None) -> httpx2.Client:
    def wrapped(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    return build_client(Allowlist(set()), transport=httpx2.MockTransport(wrapped))


def business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# --- readers ----------------------------------------------------------------------------------------------
def test_readers() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        host = request.url.host
        if host == "api.eia.gov":
            return httpx2.Response(200, json={"response": {"data": [{"period": "2026-10-16", "value": "71.2"},
                                                                    {"period": "2026-10-17", "value": None}]}})
        if host == "api.stlouisfed.org":
            return httpx2.Response(200, json={"observations": [{"date": "2026-09-01", "value": "205.1"},
                                                               {"date": "2026-10-01", "value": "."}]})
        if host == "api.bls.gov":
            return httpx2.Response(200, json={"status": "REQUEST_SUCCEEDED", "Results": {"series": [
                {"seriesID": "WPU0614", "data": [{"year": "2026", "period": "M09", "value": "310.5"},
                                                 {"year": "2025", "period": "M13", "value": "300"}]}]}})
        if host == "api.census.gov":
            return httpx2.Response(200, json=[
                ["GEN_VAL_MO", "GEN_QY1_MO", "UNIT_QY1", "CTY_CODE", "COMM_LVL", "I_COMMODITY", "time"],
                ["2000000", "1000000", "KG", "-", "HS6", "292610", "2026-07"],
                ["1500000", "500000", "KG", "5800", "HS6", "292610", "2026-07"],
                ["0", "0", "KG", "-", "HS6", "292610", "2026-08"]])
        if host == "ec.europa.eu":
            return httpx2.Response(200, json={"id": ["freq", "time"], "size": [1, 2], "value": {"0": 108.2, "1": 109.0},
                                              "dimension": {"time": {"category": {"index": {"2026-07": 0,
                                                                                            "2026-08": 1}}}}})
        if host == "query1.finance.yahoo.com":
            stamp = int(datetime(2026, 10, 16, 14, 30, tzinfo=NOW.tzinfo).timestamp())
            return httpx2.Response(200, json={"chart": {"result": [{"timestamp": [stamp, stamp + 86400],
                                                                    "indicators": {"quote": [{"close": [35.1, None]}]}}]}})
        if host == "stooq.com":
            return httpx2.Response(200, content=b"Date,Open,High,Low,Close,Volume\n2026-10-16,1,1,1,2.5,0\nbad,,,,,\n")
        return httpx2.Response(404)

    c = client(handler, seen)
    assert sources.eia(c, "k" * 20, "RBRTE", date(2026, 1, 1)) == [(date(2026, 10, 16), 71.2)]
    assert sources.fred(c, "k" * 20, "WPU0614", date(2026, 1, 1)) == [(date(2026, 9, 1), 205.1)]
    assert sources.bls(c, "blskey" * 4, ["WPU0614"], 2025, 2026) == {"WPU0614": [(date(2026, 9, 1), 310.5)]}
    bls_request = next(r for r in seen if r.url.host == "api.bls.gov")
    assert bls_request.method == "POST" and "blskey" not in str(bls_request.url)  # key in the body only
    assert sources.census_unit_prices(c, None, "292610", date(2026, 1, 1)) == [(date(2026, 7, 1), 2.0, 1000000.0)]
    assert sources.eurostat(c, "sts_inpp_m", {"geo": "EU27_2020"}) == [(date(2026, 7, 1), 108.2), (date(2026, 8, 1), 109.0)]
    assert sources.yahoo(c, "TTF=F") == [(date(2026, 10, 16), 35.1)]
    assert sources.stooq(c, "cl.f") == [(date(2026, 10, 16), 2.5)]
    assert all(r.url.scheme == "https" for r in seen)


# --- storage, best series, proxy ---------------------------------------------------------------------------
def fill(conn: Any, iid: str, start: date, values: list[float], volume: float | None = None) -> None:
    days = business_days(start, len(values))
    prices.upsert(conn, iid, [(d, v, volume) if volume is not None else (d, v) for d, v in zip(days, values, strict=True)])


def test_best_series_and_proxy(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    with ctx.db.transaction() as conn:
        kinds = {(r["role"], r["validated"]) for r in prices.links(conn, material_id)}
        assert ("customs", 0) in kinds and ("factor", 1) in kinds  # proposed at validation
        assert prices.best_series(conn, material_id) is None  # nothing fetched yet, no input series
        a = prices.ensure_instrument(conn, "fred", "A", "Propylène", rank=1, frequency="daily")
        b = prices.ensure_instrument(conn, "fred", "B", "Ammoniac", rank=1, frequency="daily")
        prices.link(conn, material_id, a, "input", proposed_by="user", validated=True, node_key="propylene")
        prices.link(conn, material_id, b, "input", proposed_by="user", validated=True, node_key="ammonia")
        fill(conn, a, date(2026, 1, 5), [100.0] * 10 + [110.0] * 10)
        fill(conn, b, date(2026, 1, 5), [50.0] * 20)
        proxy = prices.best_series(conn, material_id)
        assert proxy is not None and proxy.rank == 5 and proxy.points[0][1] == 100
        assert math.isclose(proxy.points[-1][1], 105.0)  # +10 % on one of two equal-weight inputs
        # A customs series with a real volume outranks the proxy; a thin one is only a weak index (rank 4).
        customs = f"us_census_trade:{'292610'}"
        fill(conn, customs, date(2025, 1, 1), [2.0] * 12, volume=0.5)
        assert prices.best_series(conn, material_id).rank == 4  # type: ignore[union-attr]
        fill(conn, customs, date(2025, 1, 1), [2.0] * 12, volume=5000)
        assert prices.best_series(conn, material_id).rank == 3  # type: ignore[union-attr]


def test_statistics() -> None:
    days = business_days(date(2025, 1, 1), 300)
    values = [100 * (1 + 0.01 * math.sin(i / 3)) for i in range(299)] + [120.0]
    s = Series(list(zip(days, values, strict=True)), 1, "daily", "test")
    st = stats.horizon_stats(s, 1)
    assert st is not None and st["materiality_pct"] > 0 and 0 < st["base_rate_up"] < 0.5
    assert stats.anomaly_z(s) is not None and stats.anomaly_z(s) > 3  # type: ignore[operator]
    ctx = stats.market_context("x", s, "USD/t", days[-6])
    assert ctx["change_5_sessions_pct"] > 15 and set(ctx["horizons"]) == {"1d", "5d", "20d"}


def test_daily_collection_and_silence(ctx: AppContext) -> None:
    validated_chain(ctx)
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.eia.gov":
            return httpx2.Response(200, json={"response": {"data": [{"period": "2026-10-16", "value": 70}]}})
        if request.url.host == "api.census.gov":
            return httpx2.Response(200, json=[["GEN_VAL_MO", "GEN_QY1_MO", "CTY_CODE", "time"],
                                              ["900", "450", "-", "2026-07"]])
        return httpx2.Response(404)

    secret = {"eia": "e" * 20}.get
    written = collect.run(ctx.db, client(handler, seen), secret, NOW)
    assert written["eia:RBRTE"] == 1 and written["us_census_trade:292610"] == 1
    again = collect.run(ctx.db, client(handler, seen), secret, NOW + timedelta(hours=3))
    assert {k for k in again if k not in collect.SHARED_SOURCES} == set()  # not due yet
    # The exchange quote proposed for the propylene input fills its history in batches: still due meanwhile.
    assert set(again) <= {ex.source for ex in collect.EXCHANGES}
    with ctx.db.connect() as conn:
        assert "eia:RBRTE" in prices.silent_instruments(conn, date(2026, 10, 25))
        assert "us_census_trade:292610" not in prices.silent_instruments(conn, date(2026, 9, 1))


def test_market_context_reaches_the_analysis_and_outcomes(ctx: AppContext) -> None:
    from tests.test_assess import suez_event

    event_id = suez_event(ctx)
    with ctx.db.transaction() as conn:
        mid = conn.execute("SELECT material_id FROM events WHERE id = ?", (event_id,)).fetchone()[0]
        a = prices.ensure_instrument(conn, "fred", "ACN", "Acrylonitrile (test)", rank=1, frequency="daily",
                                     unit="USD/t")
        prices.link(conn, mid, a, "direct", proposed_by="user", validated=True)
        fill(conn, a, date(2025, 6, 2), [1400.0 + (i % 7) for i in range(320)])
    from goulot.llm.batch import ReplayBatchBackend

    backend = ReplayBatchBackend()
    assess.run_analyses(ctx.db, ctx.budget, None, backend, NOW, assess.Allowance())
    params = next(iter(backend.batches.values()))[0][1]
    text = params["messages"][0]["content"]
    assert "fred:ACN" in text and "materiality_pct" in text and "Aucun instrument" not in text
    # Outcomes are resolved on the best series, from the ledger alone.
    with ctx.db.transaction() as conn:
        chain.append(conn, "verdict", {"verdict": "ras", "x_weeks": None}, material_id=mid,
                     at=datetime(2026, 1, 5, 12, tzinfo=NOW.tzinfo))
    assert assess.resolve_outcomes(ctx.db, datetime(2026, 9, 1, tzinfo=NOW.tzinfo)) >= 1
    with ctx.db.connect() as conn:
        outcome = chain.entries(conn, kinds=("outcome",))[0]
    assert outcome["result"] == "correct_calm" and outcome["series"] == "Acrylonitrile (test)"
    assert json.dumps(outcome)


def test_eia_history_is_read_page_by_page() -> None:
    """The EIA API returns 5,000 rows at most per request: Brent since 1987 takes several pages."""
    days = business_days(date(1987, 5, 20), 6200)
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        offset, length = int(request.url.params["offset"]), int(request.url.params["length"])
        rows = [{"period": d.isoformat(), "value": 18.0 + i * 0.001} for i, d in enumerate(days)][offset:offset + length]
        return httpx2.Response(200, json={"response": {"data": rows}})

    points = sources.eia(client(handler, seen), "e" * 20, "RBRTE", date(1987, 1, 1))
    assert len(points) == 6200 and points[0][0] == date(1987, 5, 20) and points[-1][0] == days[-1]
    assert [r.url.params["offset"] for r in seen] == ["0", "5000"]
