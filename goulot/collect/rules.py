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


def foreign_names(material: dict[str, Any]) -> list[str]:
    """Names in German, Spanish and Italian (Wikidata labels), added for the European media feeds: rules of the
    filter, never search terms (the queries keep `material_names`)."""
    known = {fold(n) for n in material_names(material)}
    out: list[str] = []
    for name in (material.get("labels") or {}).values():
        if _usable(name) and not re.search(r"\d{3,}", name) and fold(name) not in known:
            known.add(fold(name))
            out.append(name.strip())
    return out


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

    for name in [*material_names(material), *foreign_names(material)]:
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
           "bosphore": "bosporus", "bosphorus": "bosporus", "gibraltar": "gibraltar", "mandeb": "mandeb",
           # German, Spanish and Italian spellings of the European media feeds
           "hormus": "hormuz", "malakka": "malacca", "suezkanal": "suez", "panamakanal": "panama",
           "gibilterra": "gibraltar", "bosforo": "bosporus"}


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


# Words of a disruption, folded (no accents), in the five languages of the feeds: a whole media feed is not chosen
# by a query, so a site, company or strait name alone (« BASF », « Rotterdam ») does not make an article a
# candidate; with one of these words, it does. Prefixes, except the short ambiguous words of `DISRUPTION_WORDS`;
# German « Brand » alone is left out, being an English word too.
DISRUPTION_STEMS = (
    # English
    "strike", "fire", "blaze", "explosion", "blast", "shutdown", "shut down", "outage", "closure", "flood",
    "sanction", "export ban", "export control", "export restriction", "shortage", "disrupt", "accident", "leak",
    "force majeure", "blockade", "tariff", "anti-dumping", "antidumping", "price hike", "price surge", "soar",
    "rationing", "evacuat", "attack", "earthquake", "storm", "halt", "suspend", "curtail", "production cut",
    "low water", "derail", "collapse", "bankrupt", "insolven",
    # French
    "greve", "incendie", "explos", "arret", "fermeture", "inondation", "interdiction d'export", "penurie",
    "rupture", "panne", "fuite", "suspen", "blocage", "droits de douane", "flambee", "envol", "rationnement",
    "attaque", "seisme", "tempete", "faillite", "redressement judiciaire", "basses eaux",
    # German
    "streik", "grossbrand", "stillstand", "stilllegung", "schliessung", "hochwasser", "uberschwemmung", "sanktion",
    "exportverbot", "exportkontroll", "engpass", "knappheit", "ausfall", "storung", "unfall", "hohere gewalt",
    "strafzoll", "zolle", "preisanstieg", "preissprung", "rationier", "evakuier", "angriff", "erdbeben",
    "niedrigwasser", "abschalt", "drossel", "insolvenz", "pleite",
    # Spanish
    "huelga", "incendio", "cierre", "inundacion", "sancion", "prohibicion de export", "escasez", "desabastec",
    "averia", "accidente", "fuerza mayor", "bloqueo", "arancel", "subida de precio", "racionamiento", "terremoto",
    "quiebra",
    # Italian
    "sciopero", "esplosion", "chiusura", "alluvione", "inondazion", "sanzion", "divieto di esport", "carenza",
    "scarsita", "guasto", "incidente", "forza maggiore", "blocco", "dazi", "rincar", "impennata", "razionament",
    "attacco", "fallimento",
)
DISRUPTION_WORDS = frozenset({"fuga", "alza", "duties", "zoll"})  # whole words only
_DISRUPTION = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(s) for s in DISRUPTION_STEMS) + ")")


def disrupted(text: str) -> bool:
    """`text` already folded (« ß » is kept by the folding: « Großbrand » is read « grossbrand »)."""
    text = text.replace("ß", "ss")
    return _DISRUPTION.search(text) is not None or not DISRUPTION_WORDS.isdisjoint(re.findall(r"[a-z]+", text))


def match(item: Item, rules: list[Rule], country_nodes: dict[str, str] | None = None) -> list[Rule]:
    """Rules an item triggers. `country_nodes`: node_key -> country name, for the country + material rule.

    An item of a whole media feed (`queried` False) needs the material's name, or a disruption word next to the
    site, company, country or strait it names: the search sources got that selection from their query."""
    text = fold(f"{item.title} {item.excerpt} {json.dumps(item.details, ensure_ascii=False)}")
    hits: list[Rule] = []
    by_material: dict[str, list[Rule]] = {}
    for r in rules:
        by_material.setdefault(r.material_id, []).append(r)
    needs_disruption = not item.queried and not disrupted(text)
    for material_rules in by_material.values():
        material_kw = [r for r in material_rules if r.kind == "keyword" and r.node_key is None]
        material_named = any(_contains(text, r.value) for r in material_kw)
        if needs_disruption and not material_named:
            continue
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
