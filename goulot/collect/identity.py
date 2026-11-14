"""Who operates a plant, and where exactly (docs/SPEC.md, section 4, « Registres »).

A chain names a plant by its group (« Nalk Freeport », a fictitious example); a regulator writes the subsidiary that
holds the permit (« BLUE HARBOR OPERATIONS LLC ») and the county. Without that link a site looks calm when it is only
unknown. For the
United States the EPA's Toxics Release Inventory publishes, for every plant, its legal name, its parent company, its
city, its county and its state: asked once per site, kept on the Mac, asked again every 90 days or when the chain
renames the site.

Public typical chains only (`sitematch.watch_sites`): the one word sent is the first distinctive word of a producer's
name, as the chain writes it. Nothing from Ma chaîne.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx2

from goulot.collect.base import iso
from goulot.collect.sitematch import GENERIC, LEGAL_FORMS, MIN_SINGLE_NAME, Site, _words
from goulot.db.connection import Database
from goulot.net.client import fetch

SOURCE = "epa_tri"
TRI_BY_PARENT = "https://data.epa.gov/efservice/tri_facility/standardized_parent_company/CONTAINING/{word}/JSON"
TRI_BY_NAME = "https://data.epa.gov/efservice/tri_facility/facility_name/CONTAINING/{word}/JSON"
REFRESH = timedelta(days=90)
MAX_BYTES = 3_000_000
MAX_NAMES = 6


@dataclass(frozen=True)
class Identity:
    names: tuple[str, ...] = ()  # legal names of the operator at that place
    county: str = ""
    state: str = ""  # two letters; '' when the registry does not know the site


def subject(site: Site) -> str:
    return site.subject


def legal_name(facility: str) -> str:
    """« INEOS NITRILES USA LLC           GREEN LAKE PLANT » → « INEOS NITRILES USA LLC »: the operator's own name,
    cut where the plant's name starts (several spaces, a dash) or right after the legal form."""
    head = re.split(r"\s{2,}|\s-\s|-(?=[A-Za-z])|,", facility.strip(), maxsplit=1)[0]
    words = head.split()
    for i, w in enumerate(words):
        if re.sub(r"[^a-z]", "", w.lower()) in LEGAL_FORMS and i > 0:
            return " ".join(words[:i + 1])
    return " ".join(words)


def pick(site: Site, rows: list[dict[str, object]]) -> Identity:
    """Plants of the site's group in the site's city. The group is named by the site's first distinctive word, as a
    whole word of the parent's or of the plant's name; the place is the chain's: its city or its county in the
    plant's address, or two of its own place words in the plant's name."""
    # « Point Comfort/Port Lavaca »: either city will do, by all its telling words.
    cities = [c for c in ({w for w in _words(part) if w not in GENERIC and len(w) >= 4}
                          for part in re.split(r"[,(]", site.place, maxsplit=1)[0].split("/")) if c]
    names: dict[str, None] = {}
    counties: dict[str, int] = {}
    states: dict[str, int] = {}
    for row in rows:
        facility = str(row.get("facility_name") or "")
        parent = f"{row.get('standardized_parent_company') or ''} {row.get('parent_co_name') or ''}"
        if site.lead not in _words(parent) and site.lead not in _words(facility):
            continue
        city = set(_words(str(row.get("city_name") or "")))
        named = set(_words(facility))
        district = set(_words(str(row.get("county_name") or "")))
        there = any(c <= city or c <= named or c <= district for c in cities)
        # « Green Lake » is no city: the plant's own name carries it. Words of the company itself do not count.
        there = there or len((site.places - set(site.company)) & named) >= 2
        if not there or str(row.get("fac_closed_ind") or "0") == "1":
            continue
        # The plant's name is sometimes a label of the EPA's (« FREEPORT_NALK BH »): the name it writes to is the
        # operator's.
        for name in (facility, str(row.get("mail_name") or "")):
            names.setdefault(legal_name(name))
        county = re.sub(r"\s+COUNTY$", "", str(row.get("county_name") or "").strip().upper())
        state = str(row.get("state_abbr") or "").strip().upper()
        counties[county] = counties.get(county, 0) + 1
        states[state] = states.get(state, 0) + 1
    if not states:
        return Identity()
    return Identity(tuple(n for n in names if n)[:MAX_NAMES], max(counties, key=lambda c: counties[c]),
                    max(states, key=lambda s: states[s]))


def _ask(client: httpx2.Client, url: str, word: str) -> list[dict[str, object]]:
    result = fetch(client, url.format(word=quote(word.upper(), safe="")), max_bytes=MAX_BYTES)
    if result.status != 200:
        raise ValueError(f"HTTP {result.status}")
    data = json.loads(result.content)
    if not isinstance(data, list):
        raise ValueError("unexpected answer")
    return [r for r in data if isinstance(r, dict)]


def lookup(client: httpx2.Client, site: Site) -> Identity:
    """One question to the EPA for a site, by its group; a second one by the plant's own name when the group is not
    filled in. Raises ValueError when an answer cannot be read."""
    found = pick(site, _ask(client, TRI_BY_PARENT, site.lead))
    return found if found.state else pick(site, _ask(client, TRI_BY_NAME, site.lead))


def askable(site: Site) -> bool:
    """A plant of the United States with a known city; a company without any place has no single operator."""
    return site.country_iso2 == "US" and bool(site.city) and len(site.lead) >= 3


def stored(conn: sqlite3.Connection, now: datetime
           ) -> tuple[dict[tuple[str, str], Identity], set[tuple[str, str, str]]]:
    """Identities on file, and the (site, subject) whose answer is still fresh: a renamed site is asked again."""
    known: dict[tuple[str, str], Identity] = {}
    fresh: set[tuple[str, str, str]] = set()
    for r in conn.execute("SELECT * FROM site_identities WHERE source = ?", (SOURCE,)):
        known[(r["material_id"], r["node_key"])] = Identity(tuple(json.loads(r["names"])), r["county"], r["state"])
        if now - datetime.fromisoformat(r["fetched_at"]) < REFRESH:
            fresh.add((r["material_id"], r["node_key"], r["subject"]))
    return known, fresh


def ensure(db: Database, client: httpx2.Client, sites: list[Site], now: datetime, limit: int = 2
           ) -> dict[tuple[str, str], Identity]:
    """Identities of the sites, asking the EPA for at most `limit` of those not on file. A failed question is asked
    again at the next cycle; a site the EPA does not know is remembered as such."""
    with db.connect() as conn:
        known, fresh = stored(conn, now)
    asked = 0
    for site in sites:
        key = (site.material_id, site.node_key)
        if not askable(site) or (*key, subject(site)) in fresh or asked >= limit:
            continue
        asked += 1
        try:
            found = lookup(client, site)
        except (httpx2.HTTPError, ValueError):
            known.pop(key, None)  # an answer for another name of the site says nothing of this one
            continue
        known[key] = found
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO site_identities (material_id, node_key, source, subject, names, county, state, "
                "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (material_id, node_key, source) DO UPDATE "
                "SET subject = excluded.subject, names = excluded.names, county = excluded.county, "
                "state = excluded.state, fetched_at = excluded.fetched_at",
                (*key, SOURCE, subject(site), json.dumps(list(found.names)), found.county, found.state, iso(now)))
    current = {(s.material_id, s.node_key) for s in sites if askable(s)}
    return {k: v for k, v in known.items() if k in current}


def apply(sites: list[Site], identities: dict[tuple[str, str], Identity]) -> list[Site]:
    """The sites with what their identity adds: the operator's legal names as other names of the company, and the
    county as another name of the place (it counts once, as a city's other names do)."""
    out = []
    for site in sites:
        found = identities.get((site.material_id, site.node_key))
        if found is None or not found.state:
            out.append(site)
            continue
        aliases = []
        for name in found.names:
            words = tuple(w for w in _words(name) if w not in LEGAL_FORMS)
            telling = [w for w in words if w not in GENERIC]
            if words and words != site.company and telling and (len(words) >= 2 or len(words[0]) >= MIN_SINGLE_NAME):
                aliases.append(words)
        county = tuple(_words(found.county))
        names = site.city_names | ({county} if county and not set(county) <= site.places else set())
        out.append(replace(site, aliases=tuple(dict.fromkeys(aliases)), city_names=frozenset(names)))
    return out
