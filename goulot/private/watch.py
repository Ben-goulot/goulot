"""Watch rules from Ma chaîne, built in memory at each cycle and never stored (docs/SPEC.md, sections 3 and 11).

Per site, only its watch level goes out, and never the fact that it is the user's supplier:
- « aléas seulement »: nothing leaves the Mac; official hazard measurements are compared locally (50 km zone);
- « ville et matière » (default): the city with the material's names and disruption terms;
- « ville, matière et entreprise »: the same, plus a query on the company name and its aliases.
An article found this way is stored like any public article, without the rule that found it: its link to Ma chaîne
is computed again from the event and kept encrypted (`private_links`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from goulot.chain import store
from goulot.collect.rules import MIN_KEYWORD, ZONE_RADIUS_KM, Rule, material_names
from goulot.private.mychain import MyChain

PRIVATE_KEY = "private"  # node key of the in-memory rules: never written, never a typical-chain node


@dataclass
class PrivateWatch:
    rules: list[Rule] = field(default_factory=list)
    needs_material: set[str] = field(default_factory=set)  # rule node keys matched only with the material named
    queries: list[tuple[str, list[str]]] = field(default_factory=list)

    def __repr__(self) -> str:  # never print rules or queries, even by mistake in a log
        return f"PrivateWatch({len(self.rules)} rules, {len(self.queries)} queries)"


def _q(term: str) -> str:
    term = term.replace('"', "")
    return f'"{term}"' if " " in term or "-" in term else term


def build(conn: sqlite3.Connection, chains: dict[str, MyChain], disruption_terms: str) -> PrivateWatch:
    watch = PrivateWatch()
    next_id = -1

    def add(material_id: str, kind: str, value: str, key: str, lat: float | None = None, lon: float | None = None,
            radius: float | None = None) -> None:
        nonlocal next_id
        watch.rules.append(Rule(next_id, material_id, kind, value, key, lat, lon, radius))
        next_id -= 1

    for material_id, doc in chains.items():
        material = store.get_material(conn, material_id)
        if material is None or material["status"] != "validated":
            continue
        names = material_names(material)
        named = "(" + " OR ".join(_q(n) for n in names) + ")" if names else ""
        cities: list[str] = []
        companies: list[str] = []
        for i, site in enumerate(doc.sites):
            key = f"{PRIVATE_KEY}:{i}"
            if site.lat is not None and site.lon is not None:
                add(material_id, "zone", key, key, site.lat, site.lon, ZONE_RADIUS_KM)
            if site.watch_level == "hazards":
                continue
            city = site.city.split(",")[0].strip()
            if len(city) >= MIN_KEYWORD:
                add(material_id, "keyword", city, f"{key}:city")
                watch.needs_material.add(f"{key}:city")  # a city alone is not enough: the material must be named
                cities.append(city)
            if site.watch_level == "city_material_company":
                for name in [site.company, *site.aliases]:
                    if len(name.strip()) >= MIN_KEYWORD:
                        add(material_id, "keyword", name.strip(), f"{key}:company")
                        companies.append(name.strip())
        if cities and named:
            unique = sorted(set(cities))
            for i in range(0, len(unique), 6):
                watch.queries.append(("(" + " OR ".join(_q(c) for c in unique[i:i + 6]) + f") {named} "
                                      f"({disruption_terms})", [material_id]))
        if companies:
            unique = sorted(set(companies))
            for i in range(0, len(unique), 6):
                watch.queries.append(("(" + " OR ".join(_q(c) for c in unique[i:i + 6]) + f") ({disruption_terms})",
                                      [material_id]))
    return watch
