"""What the map shows of a typical chain (docs/SPEC.md, section 8, « Carte »): located nodes, flows drawn as great
circle arcs through the straits of their probable route, producing countries shaded by world share, and the frame of
the typical chain. Public data only: Ma chaîne is drawn by the page from /api/private/, vault open.
"""

from __future__ import annotations

import itertools
import math
import sqlite3
from typing import Any

from goulot.chain import geo, places, store
from goulot.collect import hotspots

ARC_STEPS = 24  # points per great circle segment
LABEL_CHARS = 40
NATIONAL = ("export_restriction", "sanctions_trade_policy")  # a national measure covers its country
NODE_ZONE_KM = {"port": 30.0}  # sites: the 50 km watch radius; a strait: its table outline
DRAWN_MODES = ("sea", "river", "pipeline", "road", "rail", "unknown")


def _great_circle(a: tuple[float, float], b: tuple[float, float], steps: int = ARC_STEPS) -> list[list[float]]:
    """[lon, lat] points from a to b ((lat, lon) each) along the great circle."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = 2 * math.asin(math.sqrt(math.sin((lat2 - lat1) / 2) ** 2
                                + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2))
    if d < 1e-9:
        return [[a[1], a[0]], [b[1], b[0]]]
    out = []
    for i in range(steps + 1):
        f = i / steps
        k1, k2 = math.sin((1 - f) * d) / math.sin(d), math.sin(f * d) / math.sin(d)
        x = k1 * math.cos(lat1) * math.cos(lon1) + k2 * math.cos(lat2) * math.cos(lon2)
        y = k1 * math.cos(lat1) * math.sin(lon1) + k2 * math.cos(lat2) * math.sin(lon2)
        z = k1 * math.sin(lat1) + k2 * math.sin(lat2)
        out.append([math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.hypot(x, y)))])
    return out


def arc(points: list[tuple[float, float]]) -> list[list[float]]:
    """Great circle path through (lat, lon) waypoints, longitudes kept continuous across the antimeridian."""
    coords: list[list[float]] = []
    for a, b in itertools.pairwise(points):
        segment = _great_circle(a, b)
        coords.extend(segment if not coords else segment[1:])
    for i in range(1, len(coords)):
        while coords[i][0] - coords[i - 1][0] > 180:
            coords[i][0] -= 360
        while coords[i][0] - coords[i - 1][0] < -180:
            coords[i][0] += 360
    return [[round(x, 3), round(y, 3)] for x, y in coords]


def _position(n: dict[str, Any]) -> tuple[float, float] | None:
    if n["lat"] is not None and n["lon"] is not None:
        return float(n["lat"]), float(n["lon"])
    if n["node_type"] == "country" and n["country_iso2"] != "unknown":
        return places.country_point(n["country_iso2"])  # a flow from « Chine » starts inside China
    return None


def build(conn: sqlite3.Connection, material_id: str) -> dict[str, Any]:
    chain = store.load_chain(conn, material_id)
    zone = places.delivery_zone(conn)
    nodes = chain["nodes"]
    by_key = {n["node_key"]: n for n in nodes}
    shown = [n for n in nodes if n["lat"] is not None and n["lon"] is not None and n["node_type"] != "country"]
    flows = []
    for e in chain["edges"]:
        if e["transport_mode"] not in DRAWN_MODES or e["lead_time_basis"] == "route":
            continue
        a, b = by_key.get(e["from_key"]), by_key.get(e["to_key"])
        if a is None or b is None:
            continue
        start, end = _position(a), _position(b)
        if start is None or end is None:
            continue
        via = [s for s, _, _, _ in places.probable_straits([a, b], [e], zone)]
        stops = [start, *((places.chokepoints()[s]["lat"], places.chokepoints()[s]["lon"]) for s in via), end]
        flows.append({"from": e["from_key"], "to": e["to_key"], "mode": e["transport_mode"],
                      "via": [places.chokepoints()[s]["name_fr"] for s in via],
                      "approximate": a.get("coord_source") in ("country", "none") or a["node_type"] == "country"
                      or b.get("coord_source") in ("country", "none"),
                      "coords": arc(stops)})
    shares: dict[str, float] = {}
    for n in nodes:
        if n["node_type"] == "country" and n["country_iso2"] != "unknown":
            shares[n["country_iso2"]] = max(shares.get(n["country_iso2"], 0.0), float(n["global_share_pct"] or 0))
    borders = {c["iso2"]: c for c in geo._borders() or []}
    countries = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"iso2": iso, "share": share},
         "geometry": {"type": "MultiPolygon", "coordinates": [[ring] for ring in borders[iso]["polygons"]]}}
        for iso, share in sorted(shares.items()) if iso in borders]}
    located = [(float(n["lon"]), float(n["lat"])) for n in shown]
    bounds = ([min(x for x, _ in located), min(y for _, y in located), max(x for x, _ in located),
               max(y for _, y in located)] if located else None)
    return {
        "nodes": [{k: n[k] for k in ("node_key", "node_type", "name", "product", "country_iso2", "global_share_pct",
                                     "confidence", "coord_source", "added_by", "lat", "lon")} for n in shown],
        "flows": flows, "countries": countries, "bounds": bounds, "zones": [],
    }


def _label(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= LABEL_CHARS else text[:LABEL_CHARS - 1].rstrip() + "…"


def _event_sources(conn: sqlite3.Connection, event_id: str) -> list[dict[str, str]]:
    return [{"title": r["title"], "url": r["url"], "publisher": r["publisher"]} for r in conn.execute(
        "SELECT a.title, a.url, COALESCE(NULLIF(a.provider_domain, ''), a.domain) AS publisher FROM event_articles ea "
        "JOIN articles a ON a.id = ea.article_id WHERE ea.event_id = ? ORDER BY a.id LIMIT 5", (event_id,))]


def _event_rings(event: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> list[tuple[str, list[list[list[float]]],
                                                                                     tuple[float, float]]]:
    """(zone key, rings, (lat, lon) of its label) for an ongoing or preparing event."""
    linked = [nodes[k] for k in event["node_keys"] if k in nodes]
    if event["event_type"] in NATIONAL:
        iso = event["country_iso2"] if event["country_iso2"] != "unknown" else next(
            (n["country_iso2"] for n in linked if n["country_iso2"] != "unknown"), "unknown")
        border = next((c for c in geo._borders() or [] if c["iso2"] == iso), None)
        point = places.country_point(iso)
        return [(iso, border["polygons"], point)] if border and point else []
    if event["details"].get("area_wide") or str(event["details"].get("event_type", "")).upper() == "DR":
        # Spread over a territory: its one point is a mere centre, not a position.
        rings = hotspots.country_rings([str(c) for c in event["details"].get("countries", [])])
        return [("area", rings, (event["lat"], event["lon"]))] if rings and event["lat"] is not None else []
    if event["origin"] == "hazard" and event["lat"] is not None and event["lon"] is not None:
        own = float(hotspots.GDACS_RADIUS_KM.get(str(event["details"].get("event_type", "")).upper(), 100))
        return [("own", [places.circle(event["lat"], event["lon"], own)], (event["lat"], event["lon"]))]
    out = []
    for n in linked:
        if n["lat"] is None or n.get("coord_source") not in places.PRECISE:
            continue  # a node placed at the country's centre gets no zone: it would cover the whole country
        size = places.zone_radius(n) if n["node_type"] == "chokepoint" else NODE_ZONE_KM.get(
            n["node_type"], places.SITE_ZONE_KM)
        out.append((n["node_key"], [places.circle(n["lat"], n["lon"], size)], (n["lat"], n["lon"])))
    return out


def zones(conn: sqlite3.Connection, material_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Alert zones: events in progress or in preparation on the chain, then the world hot spots. Their texts come
    from the events and sources: external data, shown as text."""
    nodes = {n["node_key"]: n for n in store.load_chain(conn, material_id)["nodes"]}
    out = []
    for e in events:
        if e["status"] not in ("ongoing", "preparing"):
            continue
        for key, rings, (lat, lon) in _event_rings(e, nodes):
            out.append({"id": f"event:{e['id']}:{key}", "kind": "event", "event_id": e["id"],
                        "label": _label(e["title"]), "status": e["status_label"], "date": e["first_seen"][:10],
                        "lat": lat, "lon": lon, "rings": rings, "sources": _event_sources(conn, e["id"])})
    for h in hotspots.current(conn):
        out.append({"id": h["id"], "kind": "hotspot", "event_id": None, "label": _label(h["label"]),
                    "status": h["status"], "date": h["started_at"][:10], "lat": h["lat"], "lon": h["lon"],
                    "rings": h["rings"],
                    "sources": [{"title": h["source_name"], "url": h["source_url"], "publisher": h["source_name"]}]})
    return out


def _inside_any(lon: float, lat: float, rings: list[list[list[float]]]) -> bool:
    return any(geo._inside(x, lat, ring) for ring in rings for x in (lon, lon - 360, lon + 360))


def _node_in_zone(node: dict[str, Any], zone: dict[str, Any]) -> bool:
    if _inside_any(node["lon"], node["lat"], zone["rings"]):
        return True
    # A node known only by its country sits at the country's centre: a world hot spot in that country counts for it.
    return (zone["kind"] == "hotspot" and node.get("coord_source") not in places.PRECISE
            and node["country_iso2"] != "unknown" and geo.country_at(zone["lat"], zone["lon"]) == node["country_iso2"])


def mark_exposure(data: dict[str, Any]) -> dict[str, Any]:
    """A flow or a node inside a zone is shown as exposed. The verdict engine is not told (docs/SPEC.md, section 5).
    The map of a material keeps only the world hot spots that touch its chain; all of them are listed on the home
    screen (`world_hotspots`)."""
    for flow in data["flows"]:
        flow["exposed_by"] = [z["id"] for z in data["zones"]
                              if any(_inside_any(x, y, z["rings"]) for x, y in flow["coords"])]
    for node in data["nodes"]:
        node["exposed_by"] = [z["id"] for z in data["zones"] if _node_in_zone(node, z)]
    touching = {i for item in [*data["flows"], *data["nodes"]] for i in item["exposed_by"]}
    data["zones"] = [z for z in data["zones"] if z["kind"] != "hotspot" or z["id"] in touching]
    return data


def world_hotspots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every world hot spot, with the validated materials whose public chain it touches. Ma chaîne is not read."""
    spots = [{"id": h["id"], "label": h["label"], "status": h["status"], "date": h["started_at"][:10],
              "source_name": h["source_name"], "source_url": h["source_url"], "materials": []}
             for h in hotspots.current(conn)]
    by_id = {s["id"]: s for s in spots}
    for m in store.list_materials(conn):
        if m["status"] != "validated":
            continue
        data = build(conn, m["id"])
        data["zones"] = zones(conn, m["id"], [])
        for z in mark_exposure(data)["zones"]:
            by_id[z["id"]]["materials"].append(m["name"])
    return spots
