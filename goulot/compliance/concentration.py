"""Concentration figures of the compliance report (docs/SPEC.md, section 10), computed by code only.

- Stage of a typical-chain node: a mine extracts, a recycler recycles; any other producing node takes the stage of
  its tier. A tier is « extraction » when it holds a mine, or when it is the deepest producing tier, above tier 1,
  without any plant (raw ore known only by country). Every other producing tier is « transformation ».
- Shares are national shares of world output, as given by the chain: a country node's share wins over the sum of
  the sites of the same country and tier. They are never scaled up to 100 %: when they cover less, the index is a
  lower bound, and the report says so.
- Herfindahl-Hirschman index: sum of the squared national shares, in percent (0 to 10 000).
- European benchmark of 65 % (Regulation (EU) 2024/1252, article 5.1.b): it concerns a single third country, so
  member states of the Union are skipped when looking for the top country it applies to.
"""

from __future__ import annotations

from typing import Any

from goulot.chain.countries import name_fr

STAGES = ("extraction", "transformation", "recyclage")
STAGE_LABELS = {"extraction": "Extraction", "transformation": "Transformation", "recyclage": "Recyclage"}
LOGISTICS = {"port", "chokepoint", "waterway", "distributor"}
NOT_PRODUCING = LOGISTICS | {"material"}
EU_MEMBERS = frozenset({
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU",
    "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
})
SUM_TOLERANCE_PCT = 100.5


def hhi(shares_pct: dict[str, float]) -> int | None:
    """Sum of squared shares in percent; None without any share."""
    values = [v for v in shares_pct.values() if v > 0]
    if not values:
        return None
    return round(sum(v * v for v in values))


def hhi_band(value: int | None, bands: dict[str, int]) -> str | None:
    if value is None:
        return None
    if value < bands["low"]:
        return "faible"
    if value <= bands["high"]:
        return "modérée"
    return "forte"


def benchmark(shares_pct: dict[str, float], max_pct: float) -> dict[str, Any]:
    """The 65 % benchmark on the top third country (outside the Union)."""
    third = {c: v for c, v in shares_pct.items() if c not in EU_MEMBERS and c not in ("unknown", "??", "")}
    if not third:
        return {"country": None, "country_name": None, "share_pct": None, "status": "sans objet"}
    top = max(sorted(third), key=lambda c: third[c])
    share = round(third[top], 1)
    status = "dépassé" if share > max_pct else ("à la limite" if share == max_pct else "respecté")
    return {"country": top, "country_name": name_fr(top), "share_pct": share, "status": status}


def figures(shares_pct: dict[str, float], max_pct: float, bands: dict[str, int]) -> dict[str, Any]:
    """Top country, index, band and benchmark for one set of national shares."""
    clean = {c: float(v) for c, v in shares_pct.items() if v and v > 0}
    if not clean:
        return {"top_country": None, "top_country_name": None, "top_share_pct": None, "hhi": None, "hhi_band": None,
                "coverage_pct": 0.0, "lower_bound": False, "inconsistent": False,
                "benchmark": benchmark({}, max_pct)}
    top = max(sorted(clean), key=lambda c: clean[c])
    coverage = round(sum(clean.values()), 1)
    value = hhi(clean)
    return {
        "top_country": top, "top_country_name": name_fr(top), "top_share_pct": round(clean[top], 1),
        "hhi": value, "hhi_band": hhi_band(value, bands), "coverage_pct": coverage,
        "lower_bound": coverage < 99.5, "inconsistent": coverage > SUM_TOLERANCE_PCT,
        "benchmark": benchmark(clean, max_pct),
    }


def producing(node: dict[str, Any]) -> bool:
    return int(node["tier"]) >= 1 and node["node_type"] not in NOT_PRODUCING


def tier_stages(nodes: list[dict[str, Any]]) -> dict[int, str]:
    """Stage of each producing tier (recyclers apart)."""
    tiers: dict[int, set[str]] = {}
    for n in nodes:
        if producing(n) and n["node_type"] != "recycler":
            tiers.setdefault(int(n["tier"]), set()).add(n["node_type"])
    deepest = max(tiers, default=0)
    out = {}
    for tier, types in tiers.items():
        raw_only = tier == deepest and tier > 1 and "plant" not in types
        out[tier] = "extraction" if "mine" in types or raw_only else "transformation"
    return out


def node_stage(node: dict[str, Any], stages: dict[int, str]) -> str | None:
    if not producing(node):
        return None
    if node["node_type"] == "mine":
        return "extraction"
    if node["node_type"] == "recycler":
        return "recyclage"
    return stages.get(int(node["tier"]))


def national_shares(nodes: list[dict[str, Any]]) -> dict[str, float]:
    """Shares by country for the nodes of one tier and stage: country nodes first, sites otherwise."""
    by_country: dict[str, float] = {}
    from_country_node: set[str] = set()
    for n in nodes:
        share, country = n.get("global_share_pct"), n.get("country_iso2") or "unknown"
        if share is None or country == "unknown":
            continue
        if n["node_type"] == "country":
            if country not in from_country_node:
                by_country[country] = 0.0
                from_country_node.add(country)
            by_country[country] += float(share)
        elif country not in from_country_node:
            by_country[country] = by_country.get(country, 0.0) + float(share)
    return by_country


def stage_groups(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Producing nodes grouped by stage and tier, upstream first within a stage; one group per level of the chain."""
    stages = tier_stages(nodes)
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for n in nodes:
        stage = node_stage(n, stages)
        if stage is not None:
            groups.setdefault((stage, int(n["tier"])), []).append(n)
    out = []
    for (stage, tier), members in sorted(groups.items(), key=lambda kv: (STAGES.index(kv[0][0]), -kv[0][1])):
        products = sorted({str(m.get("product") or "") for m in members if m.get("product")})
        out.append({"stage": stage, "tier": tier, "products": products, "nodes": members,
                    "shares": national_shares(members)})
    return out
