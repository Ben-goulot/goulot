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

ARC_STEPS = 24  # points per great circle segment
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
