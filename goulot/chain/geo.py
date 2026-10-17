"""Country from coordinates, offline, with the embedded Natural Earth borders (docs/SPEC.md, section 3).

`goulot/data/countries.json`: [{"iso2": "FR", "bbox": [minlon, minlat, maxlon, maxlat], "polygons": [[[lon, lat],
…]]}] (outer rings only), reduced from Natural Earth admin 0 (public domain). Without that file the check is
skipped and the Chaîne screen says so.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "countries.json"


@lru_cache(maxsize=1)
def _borders(path: Path = DATA_FILE) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    data: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return data


def borders_available() -> bool:
    return _borders() is not None


def _inside(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def country_at(lat: float, lon: float, borders: list[dict[str, Any]] | None = None) -> str | None:
    """ISO2 of the country containing the point, or None (at sea, unknown or no border data)."""
    borders = borders if borders is not None else _borders()
    if not borders:
        return None
    for country in borders:
        minlon, minlat, maxlon, maxlat = country["bbox"]
        if not (minlon <= lon <= maxlon and minlat <= lat <= maxlat):
            continue
        if any(_inside(lon, lat, ring) for ring in country["polygons"]):
            return str(country["iso2"])
    return None


def check_coordinates(chain: dict[str, Any], borders: list[dict[str, Any]] | None = None) -> int:
    """Coordinates inside another country than `country_iso2` are dropped (lat and lon set to null).
    A point at sea (ports, straits) is kept. Returns the number of nodes corrected."""
    corrected = 0
    for n in chain["nodes"]:
        if n["lat"] is None or n["country_iso2"] == "unknown" or n["node_type"] == "chokepoint":
            continue
        found = country_at(n["lat"], n["lon"], borders)
        if found is not None and found != n["country_iso2"]:
            n["lat"] = n["lon"] = None
            n["confidence"] = "low"
            n.setdefault("anomalies", []).append(
                f"Coordonnées situées en {found}, pas en {n['country_iso2']} : position retirée."
            )
            corrected += 1
    return corrected
