"""Alert zones and world hot spots (docs/SPEC.md, section 8, « Zones d'alerte »). Frozen data, no network.

The frozen case: Hormuz nearly closed since 1 March 2026 (PortWatch: 100 transits a day for three years, then 3),
an ongoing event on the strait, and a flow from the Gulf to Rotterdam through it.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.chain import store
from goulot.collect import hotspots
from goulot.collect.base import Item
from goulot.collect.runner import ensure_rules
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from tests.conftest import login
from tests.test_screens_api import prepared

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
CLOSED_SINCE = date(2026, 3, 1)


def traffic(days: int = 1300) -> list[dict[str, Any]]:
    out = []
    for i in range(days):
        day = NOW.date() - timedelta(days=days - 1 - i)
        ms = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
        out.append({"attributes": {"date": ms, "portid": "chokepoint6", "portname": "Strait of Hormuz",
                                   "n_total": 3 if day >= CLOSED_SINCE else 100}})
        out.append({"attributes": {"date": ms, "portid": "chokepoint1", "portname": "Suez Canal", "n_total": 50}})
    return out


def portwatch_client(features: list[dict[str, Any]], seen: list[int] | None = None) -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        offset, count = int(request.url.params.get("resultOffset", 0)), int(request.url.params["resultRecordCount"])
        if seen is not None:
            seen.append(offset)
        page = features[offset:offset + count]
        return httpx2.Response(200, json={"features": page, "exceededTransferLimit": offset + count < len(features)})

    return build_client(Allowlist({"services9.arcgis.com"}), transport=httpx2.MockTransport(handler))


def test_long_closure_is_a_drop_and_steady_traffic_is_not(ctx: AppContext) -> None:
    seen: list[int] = []
    with ctx.db.transaction() as conn:
        assert hotspots.update_traffic(conn, portwatch_client(traffic(), seen), NOW) == 2600
        drops = hotspots.traffic_drops(conn)
        assert hotspots.update_traffic(conn, portwatch_client([]), NOW) == 0  # once a day
    assert seen == [0, 2000]  # read page by page
    (drop,) = drops
    assert drop["portname"] == "Strait of Hormuz" and drop["since"] == CLOSED_SINCE.isoformat()
    assert drop["usual"] == 100 and drop["ratio"] == 0.03
    # A one-year median would have taken the closure for the norm: 7 months out of 12 at 3 transits a day.
    last_year = sorted([3.0] * 240 + [100.0] * 125)
    assert last_year[len(last_year) // 2] == 3.0


def test_gdacs_orange_and_red_only() -> None:
    def alert(level: str, kind: str = "TC") -> Item:
        return Item(source="gdacs", kind="hazard", url=f"https://www.gdacs.org/report.aspx?{level}",
                    title=f"Cyclone {level}", lat=20.0, lon=120.0, details={"alert_level": level, "event_type": kind})

    spots = hotspots.gdacs_hotspots([alert("Green"), alert("Orange"), alert("Red", "EQ")])
    assert [s["status"] for s in spots] == ["alerte orange", "alerte rouge"]
    assert spots[0]["source_url"].startswith("https://www.gdacs.org/")


def test_a_failed_portwatch_answer_keeps_the_hot_spots(ctx: AppContext) -> None:
    with ctx.db.transaction() as conn:
        hotspots.update_traffic(conn, portwatch_client(traffic()), NOW - timedelta(days=1))
        hotspots._replace(conn, "strait_traffic", hotspots.strait_hotspots(conn), NOW)
    down = build_client(Allowlist({"services9.arcgis.com"}),
                        transport=httpx2.MockTransport(lambda r: httpx2.Response(503)))
    assert hotspots.refresh(ctx.db, down, NOW, {}) == 1
    with ctx.db.connect() as conn:
        (spot,) = hotspots.current(conn)
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert spot["label"] == "Ormuz : trafic à 3 % de la normale" and events == 0  # never an event by itself


def frozen_hormuz(ctx: AppContext) -> tuple[str, str]:
    """The test chain, a sea flow from Jubail to Rotterdam, an ongoing event on Hormuz, PortWatch's drop."""
    material_id, _ = prepared(ctx)
    now = NOW.isoformat()
    with ctx.db.transaction() as conn:
        conn.execute(store.INSERT_NODE, [{
            "material_id": material_id, "node_key": "port_jubail", "node_type": "port", "name": "Port de Jubail",
            "product": "ammoniac", "tier": 2, "country_iso2": "SA", "place": "Jubail", "lat": 27.0, "lon": 49.66,
            "global_share_pct": None, "buffer_days": None, "buffer_is_default": 0, "capacity_text": "unknown",
            "confidence": "medium", "notes": "", "anomalies": "[]", "user_modified": 0, "valid_from": now,
            "valid_to": None, "recorded_at": now, "coord_source": "ai", "added_by": ""}[c] for c in store.NODE_COLUMNS])
        conn.execute(store.INSERT_EDGE, [{
            "material_id": material_id, "from_key": "port_jubail", "to_key": "port_rotterdam", "relation": "ships_via",
            "transport_mode": "sea", "lead_time_days": 25.0, "lead_time_basis": "source", "share_pct": None,
            "confidence": "medium", "anomalies": "[]", "user_modified": 0, "valid_from": now, "valid_to": None,
            "recorded_at": now}[c] for c in store.EDGE_COLUMNS])
        conn.execute("UPDATE materials SET status = 'validated' WHERE id = ?", (material_id,))
        store.refresh_rules(conn, material_id)
        ensure_rules(conn)
        conn.execute(
            "INSERT INTO events (id, material_id, event_type, event_status, origin, title, place, country_iso2, "
            "first_seen_at, last_seen_at, c_fait, lineages, confirmation, severity_hint, updated_at) VALUES "
            "('EVT-2026-10-24-0001', ?, 'maritime_disruption', 'ongoing', 'news', "
            "'Ormuz : trafic quasi nul, nouvelles attaques contre des pétroliers', 'Détroit d''Ormuz', 'unknown', "
            "?, ?, 0.9, 2, 'confirmed', 3, ?)", (material_id, now, now, now))
        conn.execute("INSERT INTO event_links (event_id, material_id, node_key, supply_share_pct) "
                     "VALUES ('EVT-2026-10-24-0001', ?, 'route_hormuz', NULL)", (material_id,))
        hotspots.update_traffic(conn, portwatch_client(traffic()), NOW)
        hotspots._replace(conn, "strait_traffic", hotspots.strait_hotspots(conn), NOW)
    return material_id, "EVT-2026-10-24-0001"


def test_frozen_hormuz_shows_zone_label_and_exposed_flow(client: TestClient, ctx: AppContext) -> None:
    material_id, event_id = frozen_hormuz(ctx)
    login(client, ctx)
    data = client.get(f"/api/materials/{material_id}/map").json()
    zones = {z["id"]: z for z in data["zones"]}
    event_zone = zones[f"event:{event_id}:route_hormuz"]
    assert event_zone["label"] == "Ormuz : trafic quasi nul, nouvelles att…" and len(event_zone["label"]) == 40
    assert event_zone["status"] == "En cours" and event_zone["date"] == "2026-10-27"
    hot = zones["strait_traffic:hormuz"]
    assert hot["kind"] == "hotspot" and hot["label"] == "Ormuz : trafic à 3 % de la normale"
    assert hot["sources"][0]["url"].startswith("https://portwatch.imf.org/")
    (flow,) = [f for f in data["flows"] if f["from"] == "port_jubail"]
    assert {event_zone["id"], hot["id"]} <= set(flow["exposed_by"])  # also the test flood at Rotterdam
    jubail = next(n for n in data["nodes"] if n["node_key"] == "port_jubail")
    assert jubail["exposed_by"] == []  # Jubail itself is outside both zones
    # The hot spot is on every map, also for a chain that does not go through Hormuz.
    assert json.dumps(hot["rings"]) in json.dumps(data["zones"])


def test_hot_spots_do_not_change_the_verdict(client: TestClient, ctx: AppContext) -> None:
    prepared(ctx)
    login(client, ctx)
    before = client.get("/api/markets").json()["materials"][0]["verdict"]
    with ctx.db.transaction() as conn:
        hotspots.update_traffic(conn, portwatch_client(traffic()), NOW)
        hotspots._replace(conn, "strait_traffic", hotspots.strait_hotspots(conn), NOW)
    from goulot.pipeline import assess

    assess.refresh_verdicts(ctx.db, ctx.vault, NOW, verified=True, last_ok_cycle=NOW.isoformat())
    after = client.get("/api/markets").json()["materials"][0]["verdict"]
    assert {k: before[k] for k in ("verdict", "x_weeks")} == {k: after[k] for k in ("verdict", "x_weeks")}
