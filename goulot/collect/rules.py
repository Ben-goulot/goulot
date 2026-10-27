"""Watch rules of a validated typical chain, and the free rule filter (docs/SPEC.md, sections 3 and 4).

Rules: names of sites, companies, ports, straits and waterways; names of the material (French label and its
English synonyms from PubChem); producing countries combined with the material name; a 50 km zone around each
located site; HS codes. Nothing from Ma chaîne is ever stored here (phase 4 computes those rules in memory).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from goulot.chain.leadtimes import great_circle_km
from goulot.collect.base import Item, fold
from goulot.db.connection import utcnow

ZONE_RADIUS_KM = 50.0
SITE_TYPES = ("mine", "plant", "recycler", "company", "port", "distributor")
MIN_KEYWORD = 4  # shorter names (« BP », « SQM ») match too much; they stay in the chain, not in the filter
MAX_MATERIAL_NAMES = 4


@dataclass(frozen=True)
class Rule:
    id: int
    material_id: str
    kind: str
    value: str
    node_key: str | None
    lat: float | None
    lon: float | None
    radius_km: float | None


def _usable(name: str) -> bool:
    return len(name.strip()) >= MIN_KEYWORD and name.strip().lower() != "unknown"


def material_names(material: dict[str, Any]) -> list[str]:
    names = [material["name"], *material.get("synonyms", [])]
    out: list[str] = []
    for n in names:
        if _usable(n) and fold(n) not in {fold(o) for o in out} and not re.search(r"\d{3,}", n):
            out.append(n.strip())
    return out[:MAX_MATERIAL_NAMES]


def generate(conn: sqlite3.Connection, material: dict[str, Any], nodes: list[dict[str, Any]]) -> int:
    """Replace the active rules of a material by those of its current validated chain. Returns the count."""
    from goulot.chain import places

    now = utcnow()
    conn.execute("UPDATE watch_rules SET active = 0 WHERE material_id = ?", (material["id"],))
    rows: list[tuple[Any, ...]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, node_key: str | None = None, lat: float | None = None,
            lon: float | None = None, radius: float | None = None) -> None:
        key = (kind, fold(value) if kind in ("keyword", "strait") else value)
        if key in seen:
            return
        seen.add(key)
        rows.append((material["id"], kind, value, node_key, lat, lon, radius, now))

    for name in material_names(material):
        add("keyword", name)
    for n in nodes:
        if n["node_type"] in SITE_TYPES:
            # « Yichun, Jiangxi » is written « Yichun » by the press: the city alone is a rule too.
            for name in (n["name"], n["place"], n["place"].split(",")[0]):
                if _usable(name):
                    add("keyword", name, n["node_key"])
        if n["node_type"] in ("chokepoint", "waterway") and _usable(n["name"]):
            add("strait", n["name"], n["node_key"])
            known = places.match_chokepoint(n["name"], n["place"]) if n["node_type"] == "chokepoint" else None
            for name in (known["name_fr"], known["portwatch_name"]) if known else ():
                add("strait", name, n["node_key"])
        if n["node_type"] == "country" and n["country_iso2"] != "unknown":
            add("country", n["country_iso2"], n["node_key"])
            if _usable(n["name"]):
                add("keyword", n["name"], n["node_key"])  # country name, used only together with the material
        # A position known only to the country would raise false alerts all over it: no zone (docs/SPEC.md, sec. 3).
        if n["lat"] is not None and n["lon"] is not None and n.get("coord_source") in places.PRECISE:
            add("zone", n["node_key"], n["node_key"], n["lat"], n["lon"], places.zone_radius(n))
    for hs in material.get("hs_codes", []):
        add("hs_code", str(hs["code"]))
    conn.executemany(
        "INSERT INTO watch_rules (material_id, kind, value, node_key, lat, lon, radius_km, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def active_rules(conn: sqlite3.Connection) -> list[Rule]:
    return [Rule(r["id"], r["material_id"], r["kind"], r["value"], r["node_key"], r["lat"], r["lon"], r["radius_km"])
            for r in conn.execute("SELECT * FROM watch_rules WHERE active = 1 ORDER BY id")]


def _contains(text: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(fold(phrase)) + r"(?![a-z0-9])", text) is not None


GENERIC_WORDS = frozenset({
    "canal", "canals", "strait", "straits", "detroit", "detroits", "de", "du", "des", "d", "of", "the", "la", "le",
    "channel", "passage", "cape", "cap", "bab", "el", "al", "fleuve", "river", "riviere", "voie", "fluviale",
})
ALIASES = {"ormuz": "hormuz", "malaca": "malacca", "rhein": "rhin", "rhine": "rhin", "panama": "panama",
           "bosphore": "bosporus", "bosphorus": "bosporus", "gibraltar": "gibraltar", "mandeb": "mandeb"}


def proper_tokens(name: str) -> set[str]:
    """Distinctive words of a strait or waterway name, the same in French and English."""
    words = re.findall(r"[a-z0-9]+", fold(name))
    return {ALIASES.get(w, w) for w in words if w not in GENERIC_WORDS and len(w) > 2}


def _names_strait(text: str, name: str) -> bool:
    wanted = proper_tokens(name)
    return bool(wanted) and wanted <= {ALIASES.get(w, w) for w in re.findall(r"[a-z0-9]+", text)}


def _has_hs(text: str, code: str) -> bool:
    """« 2926.10 », « 292610 » or « 2926 10 » in a policy text."""
    return re.search(r"(?<!\d)" + re.escape(code[:4]) + r"[. ]?" + re.escape(code[4:6]), text) is not None


def _other_rule_hit(r: Rule, item: Item, text: str) -> bool:
    if r.kind == "strait":
        return _names_strait(text, r.value)
    return r.kind == "hs_code" and item.kind == "policy" and _has_hs(text, r.value)


def match(item: Item, rules: list[Rule], country_nodes: dict[str, str] | None = None) -> list[Rule]:
    """Rules an item triggers. `country_nodes`: node_key -> country name, for the country + material rule."""
    text = fold(f"{item.title} {item.excerpt} {json.dumps(item.details, ensure_ascii=False)}")
    hits: list[Rule] = []
    by_material: dict[str, list[Rule]] = {}
    for r in rules:
        by_material.setdefault(r.material_id, []).append(r)
    for material_rules in by_material.values():
        material_kw = [r for r in material_rules if r.kind == "keyword" and r.node_key is None]
        material_named = any(_contains(text, r.value) for r in material_kw)
        for r in material_rules:
            if r.kind == "zone" and item.lat is not None and item.lon is not None and r.lat is not None:
                if great_circle_km(item.lat, item.lon, r.lat, r.lon or 0.0) <= (r.radius_km or ZONE_RADIUS_KM):
                    hits.append(r)
            elif r.kind == "keyword" and _contains(text, r.value):
                is_country_name = country_nodes is not None and r.node_key in country_nodes
                if not is_country_name or material_named:  # a country name alone is not enough
                    hits.append(r)
            elif _other_rule_hit(r, item, text):
                hits.append(r)
    return hits
