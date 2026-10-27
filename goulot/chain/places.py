"""Positions of the typical chain's nodes, offline and without AI (docs/SPEC.md, section 3, « Positions »).

Order of precision, kept with the node in `coord_source`:
1. `user`: set by the user on the Chaîne screen;
2. `ai`: given by the reconstruction (and kept by the border check);
3. `reference`: embedded tables of the 28 IMF PortWatch chokepoints and of the main waterways;
4. `city`: GeoNames city named by the node's place (whole, then each segment), in the node's country, most populated;
5. `country`: a point inside the country. Approximate: it never creates a zone rule, and the screens say so.
`none`: no country, or a node that has no place (material, country).

Probable route straits: a sea (or unknown mode) flow from a node of known country to a node of another region, or to
the purchased material (then to the delivery zone set by the user), crosses the straits of `ROUTES`. Each strait is
added as a chain node (`added_by = 'goulot'`, « route probable, ajoutée par Goulot ») linked to the flow's
destination by an edge without lead time (`lead_time_basis = 'route'`), so that the watch, the triage and the delay X
treat it like any node. Regenerated with the rules: a strait no longer on a route is closed (bitemporal).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from goulot.chain import geo
from goulot.collect.base import fold
from goulot.collect.rules import proper_tokens
from goulot.db.connection import get_setting, utcnow
from goulot.geo import cities

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PRECISE = ("user", "ai", "reference", "city")
LOCATED_TYPES = ("mine", "plant", "recycler", "company", "port", "distributor", "chokepoint", "waterway")
SITE_ZONE_KM = 50.0
DELIVERY_ZONES = {"europe_west": "Europe de l'Ouest (mer du Nord, Manche, Atlantique)",
                  "europe_med": "Europe du Sud (Méditerranée)"}
DEFAULT_ZONE = "europe_west"
ROUTE_PREFIX = "route_"
ROUTE_NOTE = "Route probable, ajoutée par Goulot ({origin} vers {dest})."

REGION_NAMES = {
    "gulf": "golfe Persique", "east_asia": "Asie de l'Est", "southeast_asia": "Asie du Sud-Est",
    "south_asia": "Asie du Sud", "black_sea": "mer Noire", "mediterranean": "Méditerranée",
    "europe_west": "Europe de l'Ouest", "europe_north": "Baltique", "europe_med": "Europe du Sud",
    "americas_east": "Amériques (Atlantique)", "americas_west": "Amériques (Pacifique)", "oceania": "Océanie",
    "africa_east": "Afrique de l'Est", "africa_west": "Afrique de l'Ouest", "africa_south": "Afrique australe",
}
# Countries whose sea trade leaves mostly from one basin. Countries with coasts on two basins of different routes
# (Russia, Turkey, Egypt…) are left out: no strait is added for them rather than a wrong one.
REGIONS = {
    "gulf": "SA AE QA KW BH IQ IR OM",
    "east_asia": "CN JP KR KP TW HK MO MN",
    "southeast_asia": "SG MY TH VN ID PH KH MM BN LA",
    "south_asia": "IN PK BD LK",
    "black_sea": "UA GE RO BG MD",
    "mediterranean": "IT GR HR SI MT CY IL LB SY TN DZ LY MA AL ME",
    "europe_west": "FR BE NL LU DE GB IE PT CH AT CZ SK HU",
    "europe_north": "SE FI PL EE LV LT DK NO",
    "americas_east": "US CA BR AR UY PY VE CO MX GY SR TT",
    "americas_west": "CL PE EC BO",
    "oceania": "AU NZ PG NC",
    "africa_east": "KE TZ MZ MG ET DJ SO",
    "africa_west": "GN SN CI GH NG CM GA AO CD CG SL LR",
    "africa_south": "ZA NA BW ZW ZM",
}
REGION_OF = {iso: region for region, codes in REGIONS.items() for iso in codes.split()}
# The Spanish and French Mediterranean coasts trade with both basins: Spain is in the Mediterranean group, France in
# the western one. A delivery zone counts as a region of its own.
REGION_OF["ES"] = "mediterranean"
_TO_ASIA = {"gulf": ["hormuz", "malacca"], "europe_west": ["suez", "bab_el_mandeb", "malacca"],
            "mediterranean": ["suez", "bab_el_mandeb", "malacca"], "south_asia": ["malacca"],
            "africa_east": ["malacca"], "africa_west": ["cape_good_hope", "malacca"], "africa_south": ["malacca"],
            "americas_east": ["panama"], "black_sea": ["bosporus", "suez", "bab_el_mandeb", "malacca"]}
ROUTES: dict[tuple[str, str], list[str]] = {
    **{(o, "europe_west"): s for o, s in {
        "gulf": ["hormuz", "bab_el_mandeb", "suez"], "east_asia": ["malacca", "bab_el_mandeb", "suez"],
        "southeast_asia": ["malacca", "bab_el_mandeb", "suez"], "south_asia": ["bab_el_mandeb", "suez"],
        "africa_east": ["bab_el_mandeb", "suez"], "oceania": ["bab_el_mandeb", "suez"],
        "black_sea": ["bosporus", "gibraltar"], "mediterranean": ["gibraltar"], "americas_west": ["panama"],
        "europe_north": ["oresund"]}.items()},
    **{(o, "europe_med"): s for o, s in {
        "gulf": ["hormuz", "bab_el_mandeb", "suez"], "east_asia": ["malacca", "bab_el_mandeb", "suez"],
        "southeast_asia": ["malacca", "bab_el_mandeb", "suez"], "south_asia": ["bab_el_mandeb", "suez"],
        "africa_east": ["bab_el_mandeb", "suez"], "oceania": ["bab_el_mandeb", "suez"], "black_sea": ["bosporus"],
        "americas_west": ["panama", "gibraltar"], "americas_east": ["gibraltar"], "europe_west": ["gibraltar"],
        "europe_north": ["oresund", "gibraltar"], "africa_west": ["gibraltar"],
        "africa_south": ["gibraltar"]}.items()},
    **{(o, "east_asia"): s for o, s in _TO_ASIA.items()},
    **{(o, "southeast_asia"): [x for x in s if x != "malacca"] for o, s in _TO_ASIA.items()},
}
EUROPE = frozenset({"europe_west", "europe_north", "mediterranean"})
REGION_OF_ZONE = {"europe_west": "europe_west", "europe_med": "mediterranean"}


@lru_cache(maxsize=1)
def chokepoints() -> dict[str, dict[str, Any]]:
    data = json.loads((DATA_DIR / "chokepoints.json").read_text(encoding="utf-8"))
    return {c["id"]: c for c in data["chokepoints"]}


@lru_cache(maxsize=1)
def waterways() -> dict[str, dict[str, Any]]:
    data = json.loads((DATA_DIR / "waterways.json").read_text(encoding="utf-8"))
    return {w["id"]: w for w in data["waterways"]}


def _matches(name: str, names: list[str]) -> bool:
    found = proper_tokens(name)
    return bool(found) and any((wanted := proper_tokens(n)) and wanted <= found for n in names)


def match_chokepoint(*names: str) -> dict[str, Any] | None:
    return next((c for c in chokepoints().values() if any(_matches(n, c["names"]) for n in names if n)), None)


def match_waterway(*names: str) -> dict[str, Any] | None:
    return next((w for w in waterways().values() if any(_matches(n, w["names"]) for n in names if n)), None)


def circle(lat: float, lon: float, radius_km: float, sides: int = 24) -> list[list[float]]:
    """Simplified outline of a zone: a closed ring of [lon, lat] around the point."""
    dlat = radius_km / 111.2
    dlon = radius_km / (111.2 * max(math.cos(math.radians(lat)), 0.05))
    ring = [[round(lon + dlon * math.cos(2 * math.pi * i / sides), 4),
             round(lat + dlat * math.sin(2 * math.pi * i / sides), 4)] for i in range(sides)]
    return [*ring, ring[0]]


def _ring_area_centroid(ring: list[list[float]]) -> tuple[float, float, float]:
    area = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True):
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) < 1e-12:
        return 0.0, ring[0][0], ring[0][1]
    return abs(area) / 2, cx / (3 * area), cy / (3 * area)


@lru_cache(maxsize=256)
def country_point(iso2: str) -> tuple[float, float] | None:
    """A point inside the country (centroid of its largest outline, or the inside point nearest to it)."""
    borders = geo._borders()
    country = next((c for c in borders or [] if c["iso2"] == iso2), None)
    if country is None:
        return None
    ring = max(country["polygons"], key=lambda r: _ring_area_centroid(r)[0])
    _, lon, lat = _ring_area_centroid(ring)
    if geo._inside(lon, lat, ring):
        return round(lat, 3), round(lon, 3)
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    steps = 30
    grid = [(min(xs) + (max(xs) - min(xs)) * i / steps, min(ys) + (max(ys) - min(ys)) * j / steps)
            for i in range(1, steps) for j in range(1, steps)]
    candidates = [(x, y) for x, y in grid if geo._inside(x, y, ring)]
    if not candidates:
        return round(ring[0][1], 3), round(ring[0][0], 3)
    x, y = min(candidates, key=lambda p: (p[0] - lon) ** 2 + (p[1] - lat) ** 2)
    return round(y, 3), round(x, 3)


_NEAR = re.compile(r"^(?:pres d(?:e |')|near |a cote d(?:e |')|region d(?:e |'))")


def place_candidates(place: str) -> list[str]:
    """The place as written, then each of its segments: « Daishan/Zhoushan », « La Negra, près d'Antofagasta »."""
    if not place or place.strip().lower() == "unknown":
        return []
    plain = re.sub(r"\([^)]*\)", " ", place)
    out = [fold(plain).strip()]
    for part in re.split(r"[,/;]|\s-\s", plain):
        part = _NEAR.sub("", fold(part).strip())
        if part and part not in out:
            out.append(part)
    return out


def computed_position(node: dict[str, Any], city_rows: list[list[Any]] | None = None
                      ) -> tuple[float | None, float | None, str]:
    """Position from the tables, the cities or the country (ranks 3 to 5)."""
    kind, iso = node["node_type"], node["country_iso2"]
    if kind not in LOCATED_TYPES:
        return None, None, "none"
    if kind == "chokepoint":
        c = match_chokepoint(node["name"], node["place"])
        if c:
            return c["lat"], c["lon"], "reference"
    if kind == "waterway":
        w = match_waterway(node["name"], node["place"])
        if w and (iso in w["points"] or iso == "unknown"):
            lat, lon = w["points"][iso if iso in w["points"] else w["default"]]
            return lat, lon, "reference"
    if iso == "unknown":
        return None, None, "none"
    if kind not in ("chokepoint", "waterway"):
        candidates = place_candidates(node["place"])
        if kind == "port":
            candidates += place_candidates(re.sub(r"^ports? (?:de |d'|du |of )?", "", node["name"], flags=re.I))
        for candidate in candidates:
            found = cities.exact(candidate, iso, city_rows)
            if found:
                return found["lat"], found["lon"], "city"
    point = country_point(iso)
    return (point[0], point[1], "country") if point else (None, None, "none")


def locate_chain(conn: sqlite3.Connection, material_id: str, city_rows: list[list[Any]] | None = None) -> int:
    """Gives each current node its best position. Positions from the user or the AI are never replaced. Returns the
    number of nodes revised (bitemporal: a new row only when something changes)."""
    from goulot.chain.store import revise_node

    revised = 0
    rows = conn.execute("SELECT * FROM chain_nodes WHERE material_id = ? AND valid_to IS NULL ORDER BY id",
                        (material_id,)).fetchall()
    for row in rows:
        node = dict(row)
        if node["coord_source"] in ("user", "ai"):
            continue
        change: dict[str, Any]
        if node["coord_source"] == "" and node["lat"] is not None:
            change = {"coord_source": "user" if node["user_modified"] else "ai"}
        else:
            lat, lon, source = computed_position(node, city_rows)
            change = {"lat": lat, "lon": lon, "coord_source": source}
        if any(node[k] != v for k, v in change.items()):
            revise_node(conn, row, change)
            revised += 1
    return revised


def delivery_zone(conn: sqlite3.Connection) -> str:
    zone = get_setting(conn, "delivery_zone", DEFAULT_ZONE) or DEFAULT_ZONE
    return zone if zone in DELIVERY_ZONES else DEFAULT_ZONE


def probable_straits(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], zone: str
                     ) -> list[tuple[str, str, str, str]]:
    """(strait id, flow destination key, origin region, destination region) for each sea or unknown-mode flow."""
    by_key = {n["node_key"]: n for n in nodes}
    out: list[tuple[str, str, str, str]] = []
    for e in edges:
        if e["transport_mode"] not in ("sea", "unknown") or e.get("lead_time_basis") == "route":
            continue
        a, b = by_key.get(e["from_key"]), by_key.get(e["to_key"])
        if a is None or b is None or a.get("added_by") == "goulot":
            continue
        origin = REGION_OF.get(a["country_iso2"])
        if b["country_iso2"] != "unknown":
            dest = REGION_OF.get(b["country_iso2"])
        elif b["tier"] == 0:
            dest = REGION_OF_ZONE[zone]  # the purchased material goes to the user's delivery zone
        else:
            continue  # an intermediate of unknown place: no route can be told
        if origin is None or dest is None or origin == dest:
            continue
        if e["transport_mode"] == "unknown" and origin in EUROPE and dest in EUROPE:
            continue  # within Europe, a flow of unknown mode most likely goes overland
        for strait in ROUTES.get((origin, "europe_med" if dest == "mediterranean" else dest), []):
            out.append((strait, b["node_key"], origin, dest))
    return out


def apply_routes(conn: sqlite3.Connection, material_id: str, zone: str | None = None) -> int:
    """Adds or closes the probable route straits of a chain. Returns the number of changes."""
    from goulot.chain.store import EDGE_COLUMNS, INSERT_EDGE, INSERT_NODE, NODE_COLUMNS, load_chain

    zone = zone or delivery_zone(conn)
    chain = load_chain(conn, material_id)
    nodes, edges = chain["nodes"], chain["edges"]
    existing = {}  # strait id -> node key already in the chain (from the reconstruction or added earlier)
    for n in nodes:
        if n["node_type"] == "chokepoint":
            c = match_chokepoint(n["name"], n["place"])
            if c and (c["id"] not in existing or n.get("added_by") != "goulot"):
                existing[c["id"]] = n["node_key"]
    wanted: dict[tuple[str, str], tuple[str, str]] = {}
    for strait, to_key, origin, dest in probable_straits(nodes, edges, zone):
        wanted.setdefault((strait, to_key), (origin, dest))
    keys = {s: existing.get(s, ROUTE_PREFIX + s) for s, _ in wanted}
    wanted_edges = {(keys[s], to) for s, to in wanted}
    now = utcnow()
    changes = 0
    for e in edges:
        if e["lead_time_basis"] == "route" and (e["from_key"], e["to_key"]) not in wanted_edges:
            conn.execute("UPDATE chain_edges SET valid_to = ? WHERE id = ?", (now, e["id"]))
            changes += 1
    current_edges = {(e["from_key"], e["to_key"]) for e in edges
                     if e["lead_time_basis"] != "route" or (e["from_key"], e["to_key"]) in wanted_edges}
    by_key = {n["node_key"]: n for n in nodes}
    for n in nodes:
        if n.get("added_by") == "goulot" and n["node_key"] not in keys.values():
            conn.execute("UPDATE chain_nodes SET valid_to = ? WHERE material_id = ? AND node_key = ? "
                         "AND valid_to IS NULL", (now, material_id, n["node_key"]))
            changes += 1
    for (strait, to_key), (origin, dest) in wanted.items():
        key = keys[strait]
        if key not in by_key:
            c = chokepoints()[strait]
            row = {
                "material_id": material_id, "node_key": key, "node_type": "chokepoint", "name": c["name_fr"],
                "product": "Route maritime", "tier": by_key[to_key]["tier"] + 1, "country_iso2": "unknown",
                "place": c["name_fr"], "lat": c["lat"], "lon": c["lon"], "global_share_pct": None,
                "buffer_days": None, "buffer_is_default": 0, "capacity_text": "unknown", "confidence": "low",
                "notes": ROUTE_NOTE.format(origin=REGION_NAMES[origin], dest=REGION_NAMES[dest]),
                "anomalies": "[]", "user_modified": 0, "valid_from": now, "valid_to": None, "recorded_at": now,
                "coord_source": "reference", "added_by": "goulot",
            }
            conn.execute(INSERT_NODE, [row[c] for c in NODE_COLUMNS])
            by_key[key] = row
            changes += 1
        if (key, to_key) not in current_edges:
            edge = {
                "material_id": material_id, "from_key": key, "to_key": to_key, "relation": "ships_via",
                "transport_mode": "sea", "lead_time_days": 0.0, "lead_time_basis": "route", "share_pct": None,
                "confidence": "low", "anomalies": "[]", "user_modified": 0, "valid_from": now, "valid_to": None,
                "recorded_at": now,
            }
            conn.execute(INSERT_EDGE, [edge[c] for c in EDGE_COLUMNS])
            current_edges.add((key, to_key))
            changes += 1
    return changes


def zone_radius(node: dict[str, Any]) -> float:
    if node["node_type"] == "chokepoint":
        c = match_chokepoint(node["name"], node["place"])
        if c:
            return float(c["radius_km"])
    return SITE_ZONE_KM
