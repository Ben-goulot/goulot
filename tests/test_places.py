"""Positions of the typical chain and probable route straits (docs/SPEC.md, section 3, « Positions »). Offline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from goulot.api.context import AppContext
from goulot.chain import geo, places, store
from goulot.chain.delays import delays_to_material
from goulot.chain.store import ChainStateError
from goulot.collect.rules import active_rules
from goulot.collect.runner import ensure_rules
from goulot.db.connection import set_setting
from tests.test_collect import NOW, validated_chain

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
TEST_CAS = ("107-13-1", "554-13-2", "7440-55-3")
MUST_BE_PLACED = ("plant", "mine", "recycler", "port", "chokepoint", "waterway")


def recorded_chain(cas: str) -> dict[str, Any]:
    """The structured chain recorded for a test CAS (the API answer holds it as JSON text)."""
    def find(o: Any) -> dict[str, Any] | None:
        if isinstance(o, dict):
            if isinstance(o.get("nodes"), list):
                return o
            return next((r for v in o.values() if (r := find(v))), None)
        if isinstance(o, list):
            return next((r for v in o if (r := find(v))), None)
        if isinstance(o, str) and o.lstrip().startswith("{") and '"nodes"' in o:
            return find(json.loads(o))
        return None

    found = find(json.loads((FIXTURES / cas / f"chain_structure_{cas}.json").read_text(encoding="utf-8")))
    assert found is not None
    return found


@pytest.mark.parametrize("cas", TEST_CAS)
def test_every_site_of_the_test_chains_is_placed_in_its_country(cas: str) -> None:
    chain = recorded_chain(cas)
    placed = 0
    for n in chain["nodes"]:
        if n["node_type"] not in MUST_BE_PLACED:
            continue
        lat, lon, source = places.computed_position(n) if n["lat"] is None else (n["lat"], n["lon"], "ai")
        if n["country_iso2"] == "unknown" and n["node_type"] not in ("chokepoint", "waterway"):
            assert source == "none"  # no country, no place: shown as « position inconnue »
            continue
        assert source in ("ai", "reference", "city", "country") and lat is not None and lon is not None
        placed += 1
        if n["node_type"] != "chokepoint":
            found = geo.country_at(lat, lon)
            assert found == n["country_iso2"] or geo._near(lat, lon, n["country_iso2"], None), n["name"]
    assert placed >= 3


def test_place_segments_and_city_names() -> None:
    assert places.place_candidates("La Negra, près d'Antofagasta") == ["la negra, pres d'antofagasta", "la negra",
                                                                     "antofagasta"]
    assert places.place_candidates("Zhangjiagang (Yangtsé)")[0] == "zhangjiagang"
    assert places.place_candidates("unknown") == []
    node = {"node_type": "plant", "country_iso2": "CN", "name": "x", "place": "Daishan/Zhoushan"}
    assert places.computed_position(node)[2] == "city"
    assert places.computed_position({**node, "place": "Nulle part"})[2] == "country"
    assert places.computed_position({**node, "country_iso2": "unknown"})[2] == "none"
    assert places.computed_position({**node, "node_type": "material"})[2] == "none"


def test_reference_tables() -> None:
    table = places.chokepoints()
    assert len(table) == 28 and len({c["portwatch_name"] for c in table.values()}) == 28
    assert places.match_chokepoint("Détroit d'Ormuz")["id"] == "hormuz"  # type: ignore[index]
    assert places.match_chokepoint("Strait of Hormuz")["id"] == "hormuz"  # type: ignore[index]
    assert places.match_chokepoint("Port de Rotterdam") is None
    for w in places.waterways().values():
        for iso, (lat, lon) in w["points"].items():
            assert geo.country_at(lat, lon) == iso or geo._near(lat, lon, iso, None), (w["id"], iso)
    rhine = {"node_type": "waterway", "country_iso2": "DE", "name": "Rhin", "place": "unknown"}
    assert places.computed_position(rhine) == (50.085, 7.765, "reference")
    ring = places.circle(26.55, 56.3, 80)
    assert ring[0] == ring[-1] and len(ring) == 25


def test_country_point_is_inside_the_country() -> None:
    for iso in ("CL", "CN", "US", "JP", "NO", "ID", "FR", "AU"):
        point = places.country_point(iso)
        assert point is not None and geo.country_at(*point) == iso


def add_sea_flow(ctx: AppContext, material_id: str, iso: str = "CN") -> None:
    """A plant abroad shipping by sea to the purchased material."""
    with ctx.db.transaction() as conn:
        now = NOW.isoformat()
        conn.execute(store.INSERT_NODE, [{
            "material_id": material_id, "node_key": "plant_far", "node_type": "plant", "name": "Usine lointaine",
            "product": "acrylonitrile", "tier": 1, "country_iso2": iso, "place": "Ningbo", "lat": None, "lon": None,
            "global_share_pct": 5.0, "buffer_days": None, "buffer_is_default": 0, "capacity_text": "unknown",
            "confidence": "medium", "notes": "", "anomalies": "[]", "user_modified": 0, "valid_from": now,
            "valid_to": None, "recorded_at": now, "coord_source": "", "added_by": ""}[c] for c in store.NODE_COLUMNS])
        conn.execute(store.INSERT_EDGE, [{
            "material_id": material_id, "from_key": "plant_far", "to_key": "acrylonitrile", "relation": "supplies",
            "transport_mode": "sea", "lead_time_days": 35.0, "lead_time_basis": "source", "share_pct": None,
            "confidence": "medium", "anomalies": "[]", "user_modified": 0, "valid_from": now, "valid_to": None,
            "recorded_at": now}[c] for c in store.EDGE_COLUMNS])


def test_probable_straits_added_marked_and_watched(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    add_sea_flow(ctx, material_id)
    with ctx.db.transaction() as conn:
        ensure_rules(conn)  # the new node is located, then routes and rules follow
        chain = store.load_chain(conn, material_id)
        rules = [r for r in active_rules(conn) if r.material_id == material_id]
    nodes = {n["node_key"]: n for n in chain["nodes"]}
    assert nodes["plant_far"]["coord_source"] == "city"
    added = {k: n for k, n in nodes.items() if n["added_by"] == "goulot"}
    assert set(added) == {"route_malacca", "route_bab_el_mandeb"}  # Suez is already in the chain: reused
    assert all("Route probable, ajoutée par Goulot" in n["notes"] for n in added.values())
    route_edges = {(e["from_key"], e["to_key"]) for e in chain["edges"] if e["lead_time_basis"] == "route"}
    assert route_edges == {("route_malacca", "acrylonitrile"), ("route_bab_el_mandeb", "acrylonitrile"),
                           ("chokepoint_suez", "acrylonitrile")}
    straits = {r.value for r in rules if r.kind == "strait"}
    assert {"Détroit de Malacca", "Malacca Strait", "Détroit de Bab el-Mandeb"} <= straits
    zones = {r.node_key: r.radius_km for r in rules if r.kind == "zone"}
    assert zones["route_malacca"] == 250 and zones["plant_far"] == places.SITE_ZONE_KM
    # The delay X of an event on a route strait is that of the flow's destination.
    delays = delays_to_material(chain["nodes"], chain["edges"])
    assert delays["route_malacca"]["days"] == delays["acrylonitrile"]["days"] == 0
    # Regenerating changes nothing; another delivery zone changes the route.
    with ctx.db.transaction() as conn:
        assert places.apply_routes(conn, material_id) == 0
        set_setting(conn, "delivery_zone", "europe_med")
        store.refresh_rules(conn, material_id)
        assert places.apply_routes(conn, material_id) == 0
        kept = {n["node_key"] for n in store.load_chain(conn, material_id)["nodes"] if n["added_by"] == "goulot"}
    assert kept == {"route_malacca", "route_bab_el_mandeb"}


def test_route_strait_closed_when_the_flow_goes(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    add_sea_flow(ctx, material_id, iso="SA")
    with ctx.db.transaction() as conn:
        ensure_rules(conn)
        assert "route_hormuz" in {n["node_key"] for n in store.load_chain(conn, material_id)["nodes"]}
        conn.execute("UPDATE chain_edges SET valid_to = ? WHERE from_key = 'plant_far'", (NOW.isoformat(),))
        store.refresh_rules(conn, material_id)
        chain = store.load_chain(conn, material_id)
        history = conn.execute("SELECT COUNT(*) FROM chain_nodes WHERE node_key = 'route_hormuz'").fetchone()[0]
    assert not [n for n in chain["nodes"] if n["added_by"] == "goulot"]
    assert not [e for e in chain["edges"] if e["lead_time_basis"] == "route"]
    assert history == 1  # closed, never deleted


def test_country_position_never_makes_a_zone(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    with ctx.db.transaction() as conn:
        nodes = {n["node_key"]: n for n in store.load_chain(conn, material_id)["nodes"]}
        zones = {r.node_key for r in active_rules(conn) if r.kind == "zone"}
    gulf = nodes["plant_gulf_cracker"]  # « Côte du Golfe, Texas »: no such city
    assert gulf["coord_source"] == "country" and gulf["lat"] is not None
    assert "plant_gulf_cracker" not in zones
    assert nodes["port_rotterdam"]["coord_source"] == "ai" and "port_rotterdam" in zones
    assert nodes["waterway_rhine"]["coord_source"] == "reference" and "waterway_rhine" in zones


def test_catch_up_locates_chains_recorded_before_positions(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE chain_nodes SET coord_source = '', lat = NULL, lon = NULL WHERE node_key != "
                     "'port_rotterdam'")
        conn.execute("UPDATE chain_nodes SET coord_source = '' WHERE node_key = 'port_rotterdam'")
        conn.execute("UPDATE watch_rules SET active = 0 WHERE kind = 'zone'")
        assert ensure_rules(conn) == 1
        nodes = {n["node_key"]: n for n in store.load_chain(conn, material_id)["nodes"]}
        zones = {r.node_key for r in active_rules(conn) if r.kind == "zone"}
        assert ensure_rules(conn) == 0  # once only
    assert nodes["port_rotterdam"]["coord_source"] == "ai"  # a position already there is kept and labelled
    assert nodes["waterway_rhine"]["coord_source"] == "reference" and "waterway_rhine" in zones
    assert all(n["coord_source"] for n in nodes.values())


def test_user_moves_a_node_within_its_country_only(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    with ctx.db.transaction() as conn:
        with pytest.raises(ChainStateError, match="en FR, pas en US"):
            store.correct_node(conn, material_id, "plant_gulf_cracker", {"lat": 48.85, "lon": 2.35})
        store.correct_node(conn, material_id, "plant_gulf_cracker", {"lat": 29.73, "lon": -95.0})
        moved = {n["node_key"]: n for n in store.load_chain(conn, material_id)["nodes"]}["plant_gulf_cracker"]
        zones = {r.node_key for r in active_rules(conn) if r.kind == "zone"}
        assert moved["coord_source"] == "user" and "plant_gulf_cracker" in zones
        # A user position survives a change of place; a computed one follows it.
        store.correct_node(conn, material_id, "plant_gulf_cracker", {"place": "Houston"})
        assert {n["node_key"]: n for n in store.load_chain(conn, material_id)["nodes"]}[
            "plant_gulf_cracker"]["lat"] == 29.73
        store.correct_node(conn, material_id, "plant_gulf_cracker", {"lat": None, "lon": None})
        relocated = {n["node_key"]: n for n in store.load_chain(conn, material_id)["nodes"]}["plant_gulf_cracker"]
    assert relocated["coord_source"] == "city" and abs(relocated["lat"] - 29.76) < 0.1


def test_chain_api_gives_positions_and_city_search(client: Any, ctx: AppContext) -> None:
    from tests.conftest import login, unsafe_headers

    material_id = validated_chain(ctx)
    csrf = login(client, ctx)
    chain = client.get(f"/api/materials/{material_id}/chain").json()
    assert {n["coord_source"] for n in chain["nodes"]} >= {"ai", "country", "reference"}
    found = client.get("/api/places/cities", params={"q": "houst", "country": "US"}).json()
    assert found["results"][0]["name"] == "Houston" and "GeoNames" in found["attribution"]
    assert client.get("/api/places/cities", params={"q": "x", "country": "usa"}).status_code == 422
    assert client.get("/api/settings/delivery-zone").json()["zone"] == "europe_west"
    put = {"headers": unsafe_headers(csrf)}
    assert client.put("/api/settings/delivery-zone", json={"zone": "europe_med"}, **put).status_code == 200
    assert client.put("/api/settings/delivery-zone", json={"zone": "mars"}, **put).status_code == 422


def test_portwatch_names_compared_with_the_table(monkeypatch: pytest.MonkeyPatch, settings: Any, tmp_path: Path) -> None:
    import httpx2

    from goulot.cli import check_sources
    from goulot.net.allowlist import Allowlist
    from goulot.net.client import build_client

    names = [*[c["portwatch_name"] for c in places.chokepoints().values()][1:], "Strait of Somewhere"]
    answer = {"features": [{"attributes": {"portname": n}} for n in names]}
    monkeypatch.setattr(check_sources, "build_client", lambda allowlist: build_client(
        Allowlist({"services9.arcgis.com"}), transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=answer))))
    monkeypatch.setattr(check_sources, "SAMPLES", tmp_path)
    unknown, missing = check_sources.portwatch_names(settings)
    assert unknown == ["Strait of Somewhere"] and missing == ["Suez Canal"]
    refused = {"error": {"code": 400, "message": "Cannot perform query. Invalid query parameters."}}
    monkeypatch.setattr(check_sources, "build_client", lambda allowlist: build_client(
        Allowlist({"services9.arcgis.com"}), transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=refused))))
    with pytest.raises(ValueError, match="refused"):  # never reported as 28 names missing
        check_sources.portwatch_names(settings)


def test_sources_of_27_october_in_the_app_and_echa_left_aside() -> None:
    """Copernicus EMS and EUR-Lex answered from the Mac and joined the watch; ECHA answers robots with a
    JavaScript challenge: left aside, asked by nothing (docs/DECISIONS.md, 27 October 2026)."""
    from datetime import date

    from goulot.cli import check_sources
    from goulot.net.allowlist import load_allowlist
    from goulot.settings import REPO_ROOT

    app = load_allowlist(REPO_ROOT / "config")
    assert app.allows("mapping.emergency.copernicus.eu") and app.allows("eur-lex.europa.eu")
    assert not app.allows("echa.europa.eu") and "echa.europa.eu" not in check_sources.EXTRA_HOSTS
    assert not any("echa.europa.eu" in url for _, url in check_sources.targets(date(2026, 10, 27)))
