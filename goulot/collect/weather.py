"""Official weather alerts on the counties of the plants (docs/SPEC.md, section 4, « Alertes météo officielles »).

A registry of incidents confirms a stop the day it happens; it does not announce it. Most long stops of the Gulf
Coast came with the weather (the hurricane of August 2017, the freeze of February 2021), and the United States
National Weather Service issues its watches and warnings two to five days ahead. So the watch reads them, and sets
each one against the past of the plants it covers: « hard freeze warning on the county of this plant, which a
freeze stopped for 19 days in February 2021 ».

- Asked by state (`/alerts/active?area=TX`): nothing of the chains is sent. An alert names its area by county
  codes; each American site has its county code from the EPA's registry of plants (`identity.py`). The match is
  made on the Mac.
- Only what stops plants: hurricanes, storm surges, tropical storms, winter and ice storms, blizzards, hard
  freezes, extreme cold. A watch (possible) or a warning (expected), read from the alert's own code (VTEC), not
  from its English name, which changes over the years.
- One item per county and kind of weather, with the sites of the chains in that county and, for each, the longest
  stop it declared for that kind of weather (`records.antecedent`). A site no registry follows has an unknown past,
  and the item says so.
- The severity is computed here, never by a model: the alert's level, one more when a site of the county was
  already stopped that way, one more for a freeze in the Gulf states, whose plants are not built for cold, one
  less for a freeze elsewhere.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

import httpx2

from goulot.collect import identity, records
from goulot.collect.base import Item, clean_text, domain_of, fold, parse_datetime
from goulot.collect.sitematch import Site
from goulot.collect.sources import SourceError
from goulot.db.connection import Database
from goulot.net.client import fetch

SOURCE = "nws_alerts"
REGISTRY = "tceq_emissions"  # where the past of a plant is read, for now
REGISTRY_STATE = "TX"
ACTIVE = "https://api.weather.gov/alerts/active"
# The service asks every client to name itself; nothing here names the user.
HEADERS = {"User-Agent": "Goulot (veille d'approvisionnement, application locale)", "Accept": "application/geo+json"}
MAX_BYTES = 6_000_000
MAX_STATES = 12
# VTEC phenomenon → (kind of weather, French name, severity of a watch, severity of a warning)
PHENOMENA: dict[str, tuple[str, str, int, int]] = {
    "HU": ("tropical", "ouragan", 2, 3),
    "SS": ("tropical", "onde de tempête", 2, 3),
    "TR": ("tropical", "tempête tropicale", 1, 2),
    "WS": ("freeze", "tempête hivernale", 1, 2),
    "IS": ("freeze", "tempête de verglas", 1, 2),
    "BZ": ("freeze", "blizzard", 1, 2),
    "HZ": ("freeze", "gel dur", 1, 2),
    "EC": ("freeze", "froid extrême", 1, 2),
    "WC": ("freeze", "froid extrême", 1, 2),  # « Wind Chill », the name of « Extreme Cold » until 2024
}
# An alert without its code: the English names of the same phenomena.
NAMES = {"hurricane": "HU", "storm surge": "SS", "tropical storm": "TR", "winter storm": "WS", "ice storm": "IS",
         "blizzard": "BZ", "hard freeze": "HZ", "extreme cold": "EC", "wind chill": "WC"}
LEVELS = {"A": "veille (possible sous deux jours)", "W": "avertissement (attendu ou en cours)"}
GULF = frozenset({"TX", "LA", "MS", "AL", "FL"})  # their plants are not built for a hard freeze
ENDED = frozenset({"CAN", "EXP", "UPG"})  # cancelled, expired, replaced by a higher alert
_VTEC = re.compile(r"/[A-Z]\.([A-Z]{3})\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.(\d{4})\.")
STATES_FR = {"TX": "Texas", "LA": "Louisiane", "OH": "Ohio", "NY": "New York", "AL": "Alabama", "FL": "Floride",
             "MS": "Mississippi"}


@dataclass(frozen=True)
class Alert:
    url: str
    phenomenon: str
    level: str  # A (watch) | W (warning)
    office: str
    number: str
    counties: frozenset[str]  # five digits each
    area: frozenset[str]  # words of the names of its zones (« Coastal Brazoria; Inland Brazoria »)
    headline: str
    sent: datetime | None
    onset: datetime | None
    ends: datetime | None

    @property
    def hazard(self) -> str:
        return PHENOMENA[self.phenomenon][0]

    def base(self) -> int:
        _, _, watch, warning = PHENOMENA[self.phenomenon]
        return warning if self.level == "W" else watch


def parse(feature: dict[str, object]) -> Alert | None:
    """One alert of the service, when it is of a kind that stops plants and still stands."""
    p = feature.get("properties")
    if not isinstance(p, dict) or p.get("status") != "Actual" or p.get("messageType") == "Cancel":
        return None
    parameters, geocode = p.get("parameters"), p.get("geocode")
    vtec = parameters.get("VTEC") if isinstance(parameters, dict) else None
    same = geocode.get("SAME") if isinstance(geocode, dict) else None
    code = _VTEC.search(" ".join(str(v) for v in vtec) if isinstance(vtec, list) else "")
    if code:
        action, office, phenomenon, level, number = code.groups()
        if action in ENDED:
            return None
    else:
        event = str(p.get("event") or "").lower()
        level = "W" if event.endswith(" warning") else "A" if event.endswith(" watch") else ""
        phenomenon = NAMES.get(event.rsplit(" ", 1)[0], "")
        office, number = "", re.sub(r"\W", "", str(p.get("id") or ""))[-12:]
    if phenomenon not in PHENOMENA or level not in LEVELS:
        return None
    counties = frozenset(str(c)[1:] for c in (same if isinstance(same, list) else [])
                         if re.fullmatch(r"0\d{5}", str(c)))
    link = str(p.get("@id") or "")
    return Alert(
        link if link.startswith("https://api.weather.gov/") else "https://www.weather.gov/", phenomenon, level,
        office, number, counties, frozenset(re.findall(r"[a-z]+", fold(str(p.get("areaDesc") or "")))),
        clean_text(str(p.get("headline") or p.get("event") or ""), 240),
        parse_datetime(p.get("sent")), parse_datetime(p.get("onset") or p.get("effective")),
        parse_datetime(p.get("ends") or p.get("expires")))


def active(client: httpx2.Client, state: str) -> list[Alert]:
    """The alerts standing in a state. An answer that is not a list of alerts is an error, never a calm sky."""
    result = fetch(client, ACTIVE, params={"area": state}, headers=HEADERS, max_bytes=MAX_BYTES)
    if result.status != 200:
        raise SourceError(f"HTTP {result.status}")
    try:
        data = json.loads(result.content)
    except json.JSONDecodeError as exc:
        raise SourceError("réponse illisible") from exc
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise SourceError("réponse sans liste d'alertes")
    return [a for a in (parse(f) for f in features if isinstance(f, dict)) if a is not None]


def severity(alert: Alert, state: str, stopped_before: bool) -> int:
    """A freeze announced on the Gulf Coast is itself the anomaly (replay of February 2021: the winter storm watch
    on the county of a plant that stopped 19 days came out two and a half days before; at level 1 it would never
    have been analysed). Elsewhere plants are built for the cold."""
    level = alert.base() + (1 if stopped_before else 0)
    if alert.hazard == "freeze":
        level += 1 if state in GULF else -1
    return max(1, min(level, 3))


def _installations(conn: sqlite3.Connection, site: Site) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT ref FROM registry_installations WHERE source = ? AND material_id = ? AND node_key = ? AND subject = ?",
        (REGISTRY, site.material_id, site.node_key, site.subject))]


@dataclass
class County:
    """A county where the chains have plants."""

    name: str  # upper case, as the registries write it
    state: str
    fips: str = ""  # '' when only its name is known (from a declaration to a registry)
    lat: float | None = None
    lon: float | None = None

    def under(self, alert: Alert) -> bool:
        """By its code when it is known; else by its name among the names of the alert's zones."""
        if self.fips:
            return self.fips in alert.counties
        words = re.findall(r"[a-z]+", fold(self.name))
        return bool(words) and set(words) <= alert.area


def item(conn: sqlite3.Connection, alert: Alert, county: County, sites: list[Site], now: datetime) -> Item:
    """One alert on one county, with the sites of the chains it covers and what such weather did to each before."""
    hazard = alert.hazard
    pasts = []
    stopped = False
    for site in sorted(sites, key=lambda s: s.name):
        refs = _installations(conn, site)
        if not refs:
            pasts.append(f"{site.name} (passé inconnu : aucun registre ne suit ce site)")
            continue
        before = records.antecedent(conn, REGISTRY, refs, hazard, now)
        stopped = stopped or before is not None
        pasts.append(f"{site.name} ({records.antecedent_sentence(before, hazard, now)})")
    name = PHENOMENA[alert.phenomenon][1]
    place = county.name.title()
    state = STATES_FR.get(county.state, county.state)
    when = ""
    if alert.onset is not None:
        when = f"Période annoncée : à partir du {alert.onset:%d/%m/%Y %H:%M} UTC"
        when += f", jusqu'au {alert.ends:%d/%m/%Y %H:%M} UTC." if alert.ends is not None else "."
    names = sorted({s.name for s in sites})
    excerpt = " ".join(part for part in (
        f"Le service météorologique des États-Unis a émis : {name}, {LEVELS[alert.level]}.",
        when,
        f"Sites des chaînes dans ce comté : {' ; '.join(pasts)}.",
        f"Texte de l'alerte : {alert.headline}" if alert.headline else "",
    ) if part)
    return Item(
        source=SOURCE, kind="hazard", url=alert.url,
        title=f"Alerte météo officielle, comté de {place} ({state}) : {name}, "
              f"{'avertissement' if alert.level == 'W' else 'veille'}"[:300],
        published_at=alert.sent, lang="fr", excerpt=excerpt[:1000], domain=domain_of(alert.url),
        lat=county.lat, lon=county.lon,
        key=f"nws:{county.state}:{fold(county.name)}:{alert.phenomenon}.{alert.level}.{alert.office}.{alert.number}",
        details={"event_type": "WX", "severity": severity(alert, county.state, stopped), "hazard": hazard,
                 "phenomenon": alert.phenomenon, "level": "warning" if alert.level == "W" else "watch",
                 "county": place, "state": county.state, "sites": names, "area_wide": True,
                 "stopped_before": stopped})


def counties(conn: sqlite3.Connection, sites: list[Site], idents: dict[tuple[str, str], identity.Identity]
             ) -> dict[tuple[str, str], tuple[County, list[Site]]]:
    """(state, county) → the county and the sites of the chains there. A site's county is the EPA's; a company
    without any place has the counties of the installations a registry bound to it."""
    out: dict[tuple[str, str], tuple[County, list[Site]]] = {}

    def add(county: County, site: Site) -> None:
        known = out.setdefault((county.state, county.name), (county, []))
        if county.fips and not known[0].fips:  # the same county, now with its code and its position
            known[0].fips, known[0].lat, known[0].lon = county.fips, county.lat, county.lon
        if site not in known[1]:
            known[1].append(site)

    for site in sites:
        ident = idents.get((site.material_id, site.node_key))
        if ident is not None and ident.fips and ident.state:
            add(County(ident.county.upper(), ident.state, ident.fips, ident.lat, ident.lon), site)
            continue
        for ref in _installations(conn, site):
            for r in conn.execute("SELECT DISTINCT county FROM official_records WHERE source = ? AND ref = ? "
                                  "AND detailed = 1 AND county != ''", (REGISTRY, ref)):
                add(County(str(r["county"]).upper(), REGISTRY_STATE), site)
    return out


def collect(db: Database, client: httpx2.Client, now: datetime, sites: list[Site], identities: int = 2
            ) -> list[Item]:
    """One cycle: the alerts of the states where the chains have plants, kept when they cover a plant's county. The
    strongest alert of each kind of weather stands for its county."""
    us = [s for s in sites if s.country_iso2 == "US"]
    if not us:
        return []
    idents = identity.ensure(db, client, us, now, identities)
    with db.connect() as conn:
        where = counties(conn, us, idents)
    states = sorted({state for state, _ in where})[:MAX_STATES]
    alerts = {state: active(client, state) for state in states}
    items = []
    with db.connect() as conn:
        for (state, _name), (county, group) in sorted(where.items()):
            standing = [a for a in alerts.get(state, []) if county.under(a)]
            for hazard in sorted({a.hazard for a in standing}):
                top = max((a for a in standing if a.hazard == hazard),
                          key=lambda a: (a.level == "W", a.base(), a.phenomenon, a.number))
                items.append(item(conn, top, county, group, now))
    return items


def covered(conn: sqlite3.Connection, material_id: str, subjects: dict[str, str] | None = None
            ) -> dict[str, dict[str, object]]:
    """node_key → {covered, text}, for the American sites the EPA was asked about: the chain screen says which
    sites the weather alerts cover, and which they do not. `subjects`: as in `records.site_summaries`."""
    current = subjects if subjects is not None else records.current_subjects(conn, material_id)
    out: dict[str, dict[str, object]] = {}
    for r in conn.execute("SELECT node_key, subject, county, state, fips FROM site_identities WHERE material_id = ?",
                          (material_id,)):
        if current.get(r["node_key"]) != r["subject"]:
            continue
        out[r["node_key"]] = {"covered": True, "text": (
            f"Alertes météo officielles suivies sur son comté ({r['county'].title()}, {r['state']}).")} if r[
                "fips"] else {"covered": False, "text": (
                    "Comté inconnu du registre fédéral des usines : les alertes météo officielles ne sont pas suivies "
                    "pour ce site.")}
    return out
