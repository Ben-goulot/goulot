"""Recognizing a site of the typical chains in an official record (docs/SPEC.md, section 4, « Registres »).

Regulators, stock exchanges and emergency services write a plant's name their own way (« INEOS NITRILES USA LLC
GREEN LAKE PLANT »), never as the chain does (« INEOS Nitriles, Green Lake (Point Comfort/Port Lavaca) »). A whole
phrase cannot match, and a company name alone matches too much (« Lotte » is a chemical maker and a retailer). So:

- the company part of the node's name (before a comma or a parenthesis, legal forms removed) matches as a phrase,
  or else its first distinctive word does;
- and the record must also name the place: up to two distinctive words from the rest of the name and from the
  place. A source that is already local to the site (a city's feed, a country's exchange) implies the place: then
  the company phrase is required in full, a first word alone being too little.

A chain names a city in English or in French (« Cologne », « Anvers ») where a local source writes it its own way
(« Köln », « Antwerpen », « 여수 »). The embedded city list (GeoNames, offline) gives the other names of the chain's
city: any of them names the place, and counts once, as the city itself would.

Public typical chains only: nothing from Ma chaîne is read here. Closed sites and projects supply nothing and are
left out. A wrong site of the right company can still pass: the triage rejects « same company, other site ».
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from goulot.chain.store import NOT_PRODUCING
from goulot.collect.base import fold
from goulot.geo import cities as geo_cities

SITE_TYPES = ("mine", "plant", "recycler", "company")
LEGAL_FORMS = frozenset({
    "co", "corp", "corporation", "inc", "incorporated", "ltd", "limited", "llc", "lp", "plc", "gmbh", "ag", "kg", "sa",
    "sas", "bv", "nv", "spa", "srl", "pty", "company", "compagnie", "group", "groupe", "holding", "holdings", "the",
})
# Words that say what a site is, not which one: never enough to tell a company or a place from another.
GENERIC = LEGAL_FORMS | frozenset({
    "usine", "plant", "site", "mine", "mina", "works", "werk", "refinery", "raffinerie", "smelter", "fonderie",
    "complex", "complexe", "facility", "unit", "unite", "project", "projet", "new", "nouvelle", "nouveau",
    "chemical", "chemicals", "chimie", "chimique", "chem", "petrochemical", "petrochemicals", "petrochimie",
    "industries", "industrial", "industry", "industrie", "materials", "performance", "plastics", "energy",
    "resources", "mining", "metals", "international", "global", "usa", "america", "americas", "europe", "asia",
    "and", "et", "de", "du", "des", "la", "le", "les", "of", "in", "en", "sur", "pres", "near", "port", "city",
    "county", "province", "north", "south", "east", "west", "nord", "sud", "est", "ouest", "unknown",
})
MIN_PLACE_WORD = 4
MIN_SINGLE_NAME = 6  # a name of three or four letters alone says too little where no place can confirm
MIN_CITY_SCRIPT = 2  # another name of a city in Chinese, Japanese or Korean script: « 宁波 », « 여수 »
EAST_ASIAN = ("CJK", "HIRAGANA", "KATAKANA", "HANGUL")


@dataclass(frozen=True)
class Site:
    material_id: str
    node_key: str
    name: str
    country_iso2: str
    company: tuple[str, ...]  # company phrase, folded words, legal forms removed
    lead: str  # its first distinctive word
    places: frozenset[str]  # distinctive words of the rest of the name and of the place
    city: frozenset[str] = frozenset()  # those of `places` that come from the city, as the chain writes it
    # Other names of that city: words of a name in Latin letters, or a one-piece name in another script.
    city_names: frozenset[tuple[str, ...]] = frozenset()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def build_site(material_id: str, node_key: str, name: str, place: str, country_iso2: str) -> Site | None:
    head = re.split(r"[,(/]", name, maxsplit=1)[0]
    company = tuple(w for w in _words(head) if w not in LEGAL_FORMS)
    distinctive = [w for w in _words(name) if w not in GENERIC and len(w) >= 3]
    if not company or not distinctive:
        return None
    lead = distinctive[0]
    city = "" if place.strip().lower() == "unknown" else re.split(r"[,(]", place, maxsplit=1)[0]
    own = {w for w in _words(city) if w not in GENERIC and len(w) >= MIN_PLACE_WORD} - {lead}
    places = {w for w in distinctive[1:] if w not in GENERIC and len(w) >= MIN_PLACE_WORD} - {lead} | own
    others = frozenset(n for part in city.split("/") for n in _city_names(part.strip(), country_iso2)
                       if not set(n) <= places and lead not in n)
    return Site(material_id, node_key, name, country_iso2, company, lead, frozenset(places), frozenset(own), others)


@lru_cache(maxsize=4096)
def _city_names(city: str, country_iso2: str) -> frozenset[tuple[str, ...]]:
    """Other names of a city, from the embedded list. GeoNames writes its romanized spellings of other scripts in
    lower case (« gu long », « kln »): nobody writes a city that way, only capitalized names are kept. A small town
    missing from the list may borrow the names of a namesake: they add unlikely words, never remove a match."""
    if len(city) < 2 or len(country_iso2) != 2 or not geo_cities.available():
        return frozenset()
    out: set[tuple[str, ...]] = set()
    for name in geo_cities.names(city, country_iso2):
        folded = fold(name).strip()
        if folded.isascii():
            words = tuple(_words(folded))
            written = name[:1].isupper() and "'" not in name
            if written and len("".join(words)) >= MIN_PLACE_WORD and set(words) - GENERIC:
                out.add(words)
        elif len(folded) >= MIN_CITY_SCRIPT and all(
                any(s in unicodedata.name(c, "") for s in EAST_ASIAN) for c in name if c.isalpha()):
            out.add((folded,))
    return frozenset(out)


def watch_sites(conn: sqlite3.Connection) -> list[Site]:
    """Producing sites and companies of the validated typical chains."""
    marks = ",".join("?" * len(SITE_TYPES))
    rows = conn.execute(
        f"SELECT n.material_id, n.node_key, n.name, n.place, n.country_iso2, n.status FROM chain_nodes n "  # noqa: S608  # nosec B608 - placeholders only
        f"JOIN materials m ON m.id = n.material_id WHERE m.status = 'validated' AND n.valid_to IS NULL "
        f"AND n.added_by = '' AND n.node_type IN ({marks}) ORDER BY n.id", SITE_TYPES).fetchall()
    out = []
    for r in rows:
        if r["status"] in NOT_PRODUCING:
            continue
        site = build_site(r["material_id"], r["node_key"], r["name"], r["place"], r["country_iso2"])
        if site is not None:
            out.append(site)
    return out


def _has(words: list[str], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(tuple(words[i:i + n]) == phrase for i in range(len(words) - n + 1))


def names_site(text: str, site: Site, place_implied: bool = False, owner: str = "") -> bool:
    """`owner`: the record's own owner field, when it has one. A company whose name opens that field is named for
    sure, however short (« THE ZED CHEMICAL COMPANY » for « Zed »): no other word can be mistaken for it there."""
    folded = fold(f"{owner} {text}")
    words = re.findall(r"[a-z0-9]+", folded)
    present = set(words)
    phrase = _has(words, site.company)
    declared = [w for w in _words(owner) if w not in LEGAL_FORMS]
    owned = bool(declared) and tuple(declared[:len(site.company)]) == site.company
    whole_name = owned or (phrase and (len(site.company) >= 2 or len(site.lead) >= MIN_SINGLE_NAME))
    if place_implied:
        return whole_name
    if not site.places:  # a company without any known place: its full name or nothing
        return whole_name
    named = phrase or site.lead in present
    found = len(site.places & present)
    if not site.city & present and any(_has(words, n) if n[0].isascii() else n[0] in folded for n in site.city_names):
        found += 1  # the city under another of its names, where the chain's own spelling is absent
    return named and found >= min(2, len(site.places))


def match_sites(text: str, sites: list[Site], place_implied: bool = False, owner: str = "") -> list[Site]:
    return [s for s in sites if names_site(text, s, place_implied, owner)]
