"""What the chain map receives (docs/SPEC.md, section 8, « Carte »). Offline."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.chain import mapdata
from goulot.collect.runner import ensure_rules
from tests.conftest import login
from tests.test_places import add_sea_flow
from tests.test_screens_api import prepared


def test_arc_follows_the_great_circle_and_crosses_the_antimeridian() -> None:
    path = mapdata.arc([(35.0, 139.0), (37.0, -122.0)])  # Tokyo to San Francisco
    assert path[0] == [139.0, 35.0] and abs(path[-1][0] - 238.0) < 0.01  # continuous longitudes, no jump
    assert max(lat for _, lat in path) > 45  # the great circle bends north
    via = mapdata.arc([(30.0, 120.0), (2.5, 101.3), (51.9, 4.1)])
    assert [101.3, 2.5] in via  # passes through the waypoint


def test_map_payload(client: TestClient, ctx: AppContext) -> None:
    material_id, event_id = prepared(ctx)
    add_sea_flow(ctx, material_id)
    with ctx.db.transaction() as conn:
        ensure_rules(conn)
    login(client, ctx)
    data: dict[str, Any] = client.get(f"/api/materials/{material_id}/map").json()
    nodes = {n["node_key"]: n for n in data["nodes"]}
    assert "plant_far" in nodes and nodes["route_malacca"]["added_by"] == "goulot"
    assert all(n["node_type"] != "country" for n in data["nodes"])  # countries are shaded, not marked
    assert {f["properties"]["iso2"] for f in data["countries"]["features"]} == {"CN", "US", "DE"}
    flows = {(f["from"], f["to"]): f for f in data["flows"]}
    assert ("cn_acrylonitrile", "chokepoint_suez") in flows  # from inside China
    assert flows[("cn_acrylonitrile", "chokepoint_suez")]["approximate"] is True
    assert not [f for f in data["flows"] if f["from"].startswith("route_")]  # drawn through, not as flows
    assert ("propylene", "cn_acrylonitrile") not in flows  # no transport, no line
    west, south, east, north = data["bounds"]
    assert west <= 4.14 <= east and south <= 51.95 <= north
    (event,) = [e for e in data["events"] if e["id"] == event_id]
    assert event["status"] == "ongoing" and "chokepoint_suez" in event["node_keys"]
    assert data["zones"] == []
    assert client.get("/api/materials/not-a-uuid/map").status_code == 404


def test_flow_from_the_gulf_goes_through_hormuz(ctx: AppContext) -> None:
    from goulot.chain import store

    material_id = prepared(ctx)[0]
    with ctx.db.transaction() as conn:
        now = "2026-10-18T12:00:00+00:00"
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
        data = mapdata.build(conn, material_id)
    (flow,) = [f for f in data["flows"] if f["from"] == "port_jubail"]
    assert flow["via"] == ["Détroit d'Ormuz", "Détroit de Bab el-Mandeb", "Canal de Suez"]
    assert any(abs(x - 56.3) < 0.01 and abs(y - 26.55) < 0.01 for x, y in flow["coords"])
