"""Step 5: lead times and buffer stocks (docs/SPEC.md, section 3).

Values given by the sources are kept. Otherwise: sea legs between two located ports are estimated from the
great-circle distance × 1.3 at 13 knots (decision of 2026-10-16, instead of `searoute`); everything else takes the
defaults of the table, shown as assumptions.
"""

from __future__ import annotations

import math
from typing import Any

SEA_SPEED_KNOTS = 13.0
SEA_DETOUR_FACTOR = 1.3
KM_PER_NAUTICAL_MILE = 1.852
EARTH_RADIUS_KM = 6371.0

DEFAULT_BUFFER_DAYS: dict[str, float] = {
    "mine": 14, "plant": 21, "recycler": 21, "port": 7, "distributor": 28, "country": 21,
    "material": 0, "company": 0, "chokepoint": 0, "waterway": 0, "other": 0,
}
DEFAULT_LEAD_DAYS: dict[str, float] = {
    "road": 3, "river": 5, "sea_unknown_ports": 30, "none": 0, "rail": 7, "pipeline": 7, "air": 7, "unknown": 7,
}


def great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def sea_days(a: dict[str, Any], b: dict[str, Any]) -> float:
    km = great_circle_km(a["lat"], a["lon"], b["lat"], b["lon"]) * SEA_DETOUR_FACTOR
    return round(km / (SEA_SPEED_KNOTS * KM_PER_NAUTICAL_MILE * 24), 1)


def _located_port(node: dict[str, Any] | None) -> bool:
    return bool(node) and node["node_type"] == "port" and node["lat"] is not None and node["lon"] is not None  # type: ignore[index]


def fill_defaults(chain: dict[str, Any]) -> None:
    """Fill buffer_days and lead_time_days in place; each filled value records its basis."""
    nodes = {n["node_id"]: n for n in chain["nodes"]}
    for n in chain["nodes"]:
        n["buffer_is_default"] = n["buffer_days"] is None
        if n["buffer_days"] is None:
            n["buffer_days"] = float(DEFAULT_BUFFER_DAYS.get(n["node_type"], 0))
    sinks = {n["node_id"] for n in chain["nodes"] if n["tier"] == 0}
    for e in chain["edges"]:
        if e["lead_time_days"] is not None:
            e["lead_time_basis"] = "source"
            continue
        mode = e["transport_mode"]
        source = nodes.get(e["from_id"])
        if mode == "none" and e["to_id"] in sinks and source is not None and source["node_type"] != "material":
            # A producer linked straight to the purchased material still has to deliver it: unknown transport.
            mode = e["transport_mode"] = "unknown"
        if mode == "sea":
            a, b = nodes.get(e["from_id"]), nodes.get(e["to_id"])
            if _located_port(a) and _located_port(b):
                e["lead_time_days"], e["lead_time_basis"] = sea_days(a, b), "sea_estimate"  # type: ignore[arg-type]
                continue
            e["lead_time_days"] = float(DEFAULT_LEAD_DAYS["sea_unknown_ports"])
        else:
            e["lead_time_days"] = float(DEFAULT_LEAD_DAYS.get(mode, DEFAULT_LEAD_DAYS["unknown"]))
        e["lead_time_basis"] = "default"
