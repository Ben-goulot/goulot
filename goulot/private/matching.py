"""Local matching of events with the sites of Ma chaîne (docs/SPEC.md, section 11), on the Mac, without AI.

The entities and the place extracted by the triage, and the event's own place, are compared with the names, company,
aliases and city of each site; a hazard less than 50 km from a site touches it. Links go, encrypted, to
`private_links`; an event of the typical chain touches the sites that adopted its node through the exposure model.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from goulot.chain.leadtimes import great_circle_km
from goulot.collect.base import fold
from goulot.collect.rules import MIN_KEYWORD, ZONE_RADIUS_KM
from goulot.private import records
from goulot.private.mychain import Model, Site
from goulot.vault.keys import Vault

ACTIVE_WINDOW = timedelta(days=60)
NAMED_KINDS = {"company", "site", "port"}


def _has(text: str, phrase: str) -> bool:
    phrase = fold(phrase).strip()
    return len(phrase) >= MIN_KEYWORD and re.search(
        r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def site_hits(site: Site, names: list[str], places: list[str], lat: float | None, lon: float | None) -> list[str]:
    """How an event touches a site: « nom » (company, site or alias), « ville », « zone » (within 50 km)."""
    how = []
    own = [site.name, site.company, *site.aliases]
    if any(_has(fold(n), o) or _has(fold(o), n) for n in names for o in own if o and n):
        how.append("nom")
    city = site.city.split(",")[0]
    if city and any(_has(fold(p), city) for p in places if p and p != "unknown"):
        how.append("ville")
    if lat is not None and lon is not None and site.lat is not None and site.lon is not None \
            and great_circle_km(lat, lon, site.lat, site.lon) <= ZONE_RADIUS_KM:
        how.append("zone")
    return how


def event_facts(conn: sqlite3.Connection, event: sqlite3.Row) -> tuple[list[str], list[str]]:
    """Names (companies, sites, ports) and places of an event, from its triaged articles and from itself."""
    names: set[str] = set()
    places: set[str] = {str(event["place"] or "")}
    for (raw,) in conn.execute("SELECT t.result FROM event_articles ea JOIN triage t ON t.article_id = ea.article_id "
                               "AND t.material_id = ? AND t.state = 'done' WHERE ea.event_id = ?",
                               (event["material_id"], event["id"])):
        t = json.loads(raw)
        names |= {e["name"] for e in t.get("entities", []) if e.get("kind") in NAMED_KINDS}
        places.add(str(t.get("place", "")))
    return sorted(names - {"", "unknown"}), sorted(places - {"", "unknown"})


def match_material(conn: sqlite3.Connection, material_id: str, model: Model, now: datetime) -> dict[str, Any]:
    """event_id -> {"sites": [...], "how": [...]} for the active events of a material."""
    since = (now - ACTIVE_WINDOW).isoformat(timespec="seconds")
    links: dict[str, Any] = {}
    for e in conn.execute("SELECT * FROM events WHERE material_id = ? AND last_seen_at >= ?",
                          (material_id, since)).fetchall():
        names, places = event_facts(conn, e)
        touched, how = [], set()
        for site in model.doc.sites:
            found = site_hits(site, names, places, e["lat"], e["lon"])
            if found:
                touched.append(site.id)
                how |= set(found)
        if touched:
            links[e["id"]] = {"sites": touched, "how": sorted(how)}
    return links


def refresh(conn: sqlite3.Connection, vault: Vault, models: dict[str, tuple[int, Model]], now: datetime
            ) -> set[str]:
    """Recompute and store the private links of every Ma chaîne. Returns the events that touch a site which weighs
    for the user (at least 10 % of their volume, or their sole supplier): they trigger the analysis."""
    weighs: set[str] = set()
    for material_id, (_, model) in models.items():
        links = match_material(conn, material_id, model, now)
        records.write_links(conn, vault, material_id, links)
        for event_id, link in links.items():
            if any(model.weighs(s) for s in link["sites"]):
                weighs.add(event_id)
        for (event_id,) in conn.execute("SELECT DISTINCT l.event_id FROM event_links l JOIN events e ON e.id = "
                                        "l.event_id WHERE e.material_id = ? AND l.node_key IN (SELECT value FROM "
                                        "json_each(?))", (material_id, json.dumps(
                                            [s.node_key for s in model.doc.sites if s.node_key and model.weighs(s.id)]
                                        ))):
            weighs.add(event_id)
    return weighs
