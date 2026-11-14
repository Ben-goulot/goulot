"""One function per source of `config/sources.yaml`, all through the allowlist client (CLAUDE.md, rule 6).

Formats follow the endpoints checked on 2026-10-15 (docs/DECISIONS.md). Every function returns `Item`s and
raises `httpx2.HTTPError` or `SourceError` on failure; the runner records the error type, never the URL
(keys can sit in URLs: NASA FIRMS puts it in the path).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx2

from goulot.chain.countries import iso2_from_name
from goulot.collect.base import Item, clean_text, domain_of, iso, parse_datetime
from goulot.collect.sitematch import Site, match_sites
from goulot.net.client import fetch
from goulot.net.xml import parse_xml

log = logging.getLogger("goulot.collect")

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
GOOGLE_NEWS = "https://news.google.com/rss/search"
GDACS = "https://www.gdacs.org/xml/rss.xml"
USGS_QUAKES = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
FIRMS = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
NHC = ("https://www.nhc.noaa.gov/index-at.xml", "https://www.nhc.noaa.gov/index-ep.xml")
PORTWATCH = ("https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services/Daily_Chokepoints_Data/"
             "FeatureServer/0/query")
PEGEL = "https://www.pegelonline.wsv.de/webservices/rest-api/v2/stations/{station}/W/currentmeasurement.json"
FEDERAL_REGISTER = "https://www.federalregister.gov/api/v1/documents.json"
COPERNICUS_EMS = "https://mapping.emergency.copernicus.eu/activations/api/activations/"
COPERNICUS_PAGE = "https://mapping.emergency.copernicus.eu/activations/{code}/"
COPERNICUS_CREDIT = "© Union européenne, Copernicus Emergency Management Service"
COPERNICUS_DAYS = 30
# Copernicus categories as GDACS event types, for the size of a zone and the type of an event.
COPERNICUS_TYPES = {"flood": "FL", "fire": "WF", "wildfire": "WF", "earthquake": "EQ", "storm": "TC", "wind": "TC",
                    "volcano": "VO", "volcanic-activity": "VO", "tsunami": "TS", "drought": "DR",
                    "industrial-accident": "IND", "mass-movement": "LS", "landslide": "LS"}
EURLEX_OJ_L = "https://eur-lex.europa.eu/EN/display-feed.rss?rssId=165"

GDACS_POINT_EVENTS = {"EQ", "TC", "VO", "TS"}  # a real position, whatever the countries it reaches
GDACS_AREA_MIN_COUNTRIES = 3
GDELT_MAX_WINDOW = timedelta(days=90)
# VIIRS on NOAA-21 and NOAA-20: Suomi NPP observations end on 2 November 2026 (FIRMS notice of October 2026).
FIRMS_PRODUCTS = ("VIIRS_NOAA21_NRT", "VIIRS_NOAA20_NRT")
# GDELT answers HTTP 429 to more than one request every 5 seconds: its queries are spaced out (a cycle runs in the
# background, a minute or two more costs nothing). After a 429, the query is retried once after Retry-After, and the
# following ones wait longer; a second refusal or a timeout ends GDELT for this cycle, keeping what was read.
GDELT_PAUSE_S = 6.0
GDELT_PAUSE_AFTER_429_S = 20.0
GDELT_RETRY_AFTER_DEFAULT_S = 30.0
GDELT_RETRY_AFTER_MAX_S = 120.0
_gdelt_last = 0.0
KAUB_LOW_WATER_CM = 100  # below 1 m at Kaub, Rhine barges load partially (78 cm: critical)
PORTWATCH_DROP = 0.6  # a day under 60 % of the 30-day mean counts as a disruption signal
# The PortWatch service shares a quota of 6,000 request units a minute between all its users, PortWatch's own site
# included: on 6 November 2026 most queries were refused (code 429 in an HTTP 200 body, « retry after 60 sec »).
# A refused query is asked again after the delay it names, at most twice; then the source fails for this cycle.
ARCGIS_RETRIES = 2
ARCGIS_RETRY_DEFAULT_S = 60.0
ARCGIS_RETRY_MAX_S = 90.0
NS = {
    "georss": "http://www.georss.org/georss",
    "gdacs": "http://www.gdacs.org",
    "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#",
    "nhc": "https://www.nhc.noaa.gov",
}


class SourceError(RuntimeError):
    pass


class Throttled(SourceError):
    """HTTP 429, with the wait the server asked for (seconds, bounded)."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("HTTP 429")
        self.retry_after = retry_after


class PartialFailure(SourceError):
    """The source stopped answering midway: the items read before are kept, the run still counts as failed."""

    def __init__(self, message: str, items: list[Item]) -> None:
        super().__init__(message)
        self.items = items


def retry_after_seconds(value: str, now: datetime, default: float, ceiling: float) -> float:
    """Retry-After as seconds or as an HTTP date, bounded to [0, ceiling]; `default` when absent or unreadable."""
    value = value.strip()
    if not value:
        return default
    if value.isdigit():
        return min(float(value), ceiling)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return min(max((when - now).total_seconds(), 0.0), ceiling)


def _json(client: httpx2.Client, url: str, params: dict[str, str] | None = None) -> Any:
    result = fetch(client, url, params=params)
    if result.status != 200:
        raise SourceError(f"HTTP {result.status}")
    try:
        data = json.loads(result.content)
    except json.JSONDecodeError as exc:
        raise SourceError("invalid JSON") from exc
    refused(data)
    return data


def arcgis_query(client: httpx2.Client, url: str, params: dict[str, str]) -> Any:
    """An ArcGIS (PortWatch) query, asked again after the delay named by a quota refusal."""
    for attempt in range(ARCGIS_RETRIES + 1):
        result = fetch(client, url, params=params)
        if result.status != 200:
            raise SourceError(f"HTTP {result.status}")
        try:
            data = json.loads(result.content)
        except json.JSONDecodeError as exc:
            raise SourceError("invalid JSON") from exc
        wait = quota_wait(data)
        if wait is None or attempt == ARCGIS_RETRIES:
            refused(data)
            return data
        log.info("arcgis: quota exceeded, asked again in %.0f s", wait)
        time.sleep(wait)
    raise AssertionError("unreachable")


def quota_wait(data: Any) -> float | None:
    """Seconds to wait when ArcGIS refuses a query for its quota (code 429), None otherwise."""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict) or error.get("code") != 429:
        return None
    found = re.search(r"retry after (\d+)", " ".join(str(d) for d in error.get("details") or []), re.I)
    return min(float(found.group(1)) if found else ARCGIS_RETRY_DEFAULT_S, ARCGIS_RETRY_MAX_S)


def refused(data: Any) -> None:
    """ArcGIS (PortWatch) answers a refused query with HTTP 200 and an `error` object: read as « nothing to report »,
    it would look like a calm. A failure of the source, never a silence."""
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        raise SourceError(f"refused: {str(data['error'].get('message', ''))[:60]}")


def _xml(client: httpx2.Client, url: str, params: dict[str, str] | None = None) -> Any:
    result = fetch(client, url, params=params)
    if result.status != 200:
        raise SourceError(f"HTTP {result.status}")
    return parse_xml(result.content)


def _text(node: Any, path: str) -> str:
    found = node.find(path, NS)
    return (found.text or "").strip() if found is not None and found.text else ""


# --- news -------------------------------------------------------------------------------------------------
def gdelt(client: httpx2.Client, query: str, since: datetime, now: datetime, pause: float | None = None) -> list[Item]:
    global _gdelt_last
    start = max(since, now - GDELT_MAX_WINDOW)
    pause = GDELT_PAUSE_S if pause is None else pause
    wait = _gdelt_last + pause - time.monotonic()
    if pause > 0 and wait > 0:
        time.sleep(wait)
    _gdelt_last = time.monotonic()
    result = fetch(client, GDELT, params={
        "query": query, "mode": "artlist", "format": "json", "maxrecords": "250", "sort": "datedesc",
        "startdatetime": start.strftime("%Y%m%d%H%M%S"), "enddatetime": now.strftime("%Y%m%d%H%M%S"),
    })
    if result.status == 429:
        raise Throttled(retry_after_seconds(result.retry_after, datetime.now(UTC), GDELT_RETRY_AFTER_DEFAULT_S,
                                            GDELT_RETRY_AFTER_MAX_S))
    if result.status != 200:
        raise SourceError(f"HTTP {result.status}")
    try:
        data = json.loads(result.content)
    except json.JSONDecodeError as exc:
        raise SourceError("invalid JSON") from exc
    items = []
    for a in (data or {}).get("articles", []) or []:
        url = str(a.get("url") or "")
        if not url.startswith(("https://", "http://")):
            continue
        items.append(Item(
            source="gdelt_doc", kind="news", url=url, title=clean_text(a.get("title"), 300),
            published_at=parse_datetime(a.get("seendate")), lang=str(a.get("language") or "")[:20],
            domain=domain_of(url), details={"source_country": str(a.get("sourcecountry") or "")[:40]},
        ))
    return items


def gdelt_many(client: httpx2.Client, queries: list[str], since: datetime, now: datetime) -> list[Item]:
    """All GDELT queries of a cycle, paced. One retry after a 429; then, or after a timeout, a PartialFailure."""
    items: list[Item] = []
    pause, retried = GDELT_PAUSE_S, False
    for query in queries:
        try:
            try:
                items += gdelt(client, query, since, now, pause)
            except Throttled as exc:
                pause = max(pause, GDELT_PAUSE_AFTER_429_S)
                if retried:
                    raise
                retried = True
                log.info("gdelt: HTTP 429, retry in %.0f s", exc.retry_after)
                time.sleep(exc.retry_after)
                items += gdelt(client, query, since, now, pause)
        except Throttled as exc:
            raise PartialFailure("HTTP 429", items) from exc
        except httpx2.TimeoutException as exc:
            raise PartialFailure(type(exc).__name__, items) from exc
    return items


def google_news(client: httpx2.Client, query: str, lang: str = "en", country: str = "US") -> list[Item]:
    root = _xml(client, GOOGLE_NEWS, {"q": query, "hl": lang, "gl": country, "ceid": f"{country}:{lang}"})
    items = []
    for it in root.iter("item"):
        link = _text(it, "link")
        source = it.find("source")
        provider = domain_of(source.get("url", "")) if source is not None else ""
        title = clean_text(_text(it, "title"), 300)
        if source is not None and source.text and title.endswith(" - " + source.text.strip()):
            title = title[: -len(source.text.strip()) - 3]
        items.append(Item(
            source="google_news_rss", kind="news", url=link, title=title,
            published_at=parse_datetime(_text(it, "pubDate")), lang=lang, excerpt=clean_text(_text(it, "description")),
            domain=domain_of(link), provider_domain=provider,
        ))
    return items


# --- hazards ----------------------------------------------------------------------------------------------
def gdacs(client: httpx2.Client) -> list[Item]:
    root = _xml(client, GDACS)
    items = []
    for it in root.iter("item"):
        point = _text(it, "georss:point").split()
        lat = lon = None
        if len(point) == 2:
            lat, lon = float(point[0]), float(point[1])
        else:
            la, lo = _text(it, "geo:Point/geo:lat"), _text(it, "geo:Point/geo:long")
            if la and lo:
                lat, lon = float(la), float(lo)
        link = _text(it, "link")
        title = clean_text(_text(it, "title"), 300)
        kind = _text(it, "gdacs:eventtype")[:10].upper()
        # Several countries, comma-separated. GDACS gives one point for them: for an event spread over a territory
        # it is a mere centre, in a country that may not even be concerned, never a location.
        listed = [c.strip() for c in (_text(it, "gdacs:country") or title.partition(" in ")[2]).split(",") if c.strip()]
        countries = sorted({iso for iso in map(iso2_from_name, listed) if iso})
        area_wide = kind == "DR" or (kind not in GDACS_POINT_EVENTS and len(listed) >= GDACS_AREA_MIN_COUNTRIES)
        items.append(Item(
            source="gdacs", kind="hazard", url=link, title=title,
            published_at=parse_datetime(_text(it, "pubDate")), excerpt=clean_text(_text(it, "description")),
            domain=domain_of(link), lat=lat, lon=lon, key="gdacs:" + (_text(it, "guid") or link),
            details={"event_type": kind, "alert_level": _text(it, "gdacs:alertlevel")[:10],
                     "country": _text(it, "gdacs:country")[:80], "countries": countries, "area_wide": area_wide},
        ))
    return items


def usgs_quakes(client: httpx2.Client, min_magnitude: float = 5.0) -> list[Item]:
    data = _json(client, USGS_QUAKES)
    items = []
    for f in (data or {}).get("features", []) or []:
        p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
        mag = p.get("mag")
        if not isinstance(mag, int | float) or mag < min_magnitude or len(g) < 2:
            continue
        url = str(p.get("url") or "")
        items.append(Item(
            source="usgs_earthquakes", kind="hazard", url=url, title=clean_text(p.get("title"), 300),
            published_at=parse_datetime(p.get("time")), lat=float(g[1]), lon=float(g[0]), magnitude=float(mag),
            domain=domain_of(url), key="usgs:" + str(f.get("id") or url), details={"event_type": "EQ"},
        ))
    return items


def firms(client: httpx2.Client, key: str, boxes: Iterable[tuple[str, float, float]], days: int = 1,
          min_frp: float = 10.0) -> list[Item]:
    """Active fires near located sites. `boxes`: (node_key, lat, lon); one request per box of ±0.1° (about 11 km)
    and per satellite: only the site's surroundings, the grouping keeps what lies within 5 km. A fire seen by both
    satellites the same day, within about 1 km, is kept once."""
    items = []
    seen: set[str] = set()
    for node_key, lat, lon in boxes:
        area = f"{lon - 0.1:.3f},{lat - 0.1:.3f},{lon + 0.1:.3f},{lat + 0.1:.3f}"
        rows: list[dict[str, str]] = []
        for product in FIRMS_PRODUCTS:
            result = fetch(client, f"{FIRMS}/{quote(key, safe='')}/{product}/{area}/{days}")
            if result.status != 200:
                raise SourceError(f"HTTP {result.status}")
            rows += list(csv.DictReader(io.StringIO(result.content.decode("utf-8", "replace"))))
        for row in rows:
            try:
                flat, flon, frp = float(row["latitude"]), float(row["longitude"]), float(row.get("frp") or 0)
            except (KeyError, ValueError):
                continue
            spot = f"firms:{flat:.2f}:{flon:.2f}:{row.get('acq_date')}"
            if frp < min_frp or spot in seen:
                continue
            seen.add(spot)
            when = parse_datetime(f"{row.get('acq_date', '')}T{row.get('acq_time', '0000').zfill(4)[:2]}:"
                                  f"{row.get('acq_time', '0000').zfill(4)[2:]}:00")
            items.append(Item(
                source="nasa_firms", kind="hazard", url="https://firms.modaps.eosdis.nasa.gov/map/",
                title=f"Incendie détecté par satellite (puissance {frp:.0f} MW)", published_at=when,
                lat=flat, lon=flon, magnitude=frp, key=spot,
                details={"event_type": "WF", "near_node": node_key, "confidence": str(row.get("confidence", ""))[:10]},
            ))
    return items


def nhc(client: httpx2.Client) -> list[Item]:
    items = []
    for feed in NHC:
        root = _xml(client, feed)
        for it in root.iter("item"):
            center = _text(it, "nhc:Cyclone/nhc:center")
            lat = lon = None
            if "," in center:
                try:
                    lat, lon = (float(x) for x in center.split(",")[:2])
                except ValueError:
                    lat = lon = None
            if lat is None:
                continue  # text products without a storm position: not usable by the zone filter
            link = _text(it, "link")
            items.append(Item(
                source="noaa_nhc", kind="hazard", url=link, title=clean_text(_text(it, "title"), 300),
                published_at=parse_datetime(_text(it, "pubDate")), excerpt=clean_text(_text(it, "description")),
                lat=lat, lon=lon, domain=domain_of(link), key="nhc:" + (_text(it, "guid") or link),
                details={"event_type": "TC", "name": _text(it, "nhc:Cyclone/nhc:name")[:60],
                         "wind": _text(it, "nhc:Cyclone/nhc:wind")[:30]},
            ))
    return items


# --- logistics --------------------------------------------------------------------------------------------
def portwatch(client: httpx2.Client, now: datetime) -> list[Item]:
    """Chokepoints whose latest daily transit count falls under 60 % of their 30-day mean."""
    since = (now - timedelta(days=35)).strftime("%Y-%m-%d")
    data = arcgis_query(client, PORTWATCH, {
        "where": f"date >= DATE '{since}'", "outFields": "date,portid,portname,n_total", "orderByFields": "date",
        "resultRecordCount": "2000", "f": "json",
    })
    if not (data or {}).get("features"):
        # 28 straits counted every day: nothing over 35 days is a broken source, never a calm (6 November 2026).
        raise SourceError("no transit for 35 days")
    series: dict[str, list[tuple[datetime, float]]] = {}
    names: dict[str, str] = {}
    for feat in (data or {}).get("features", []) or []:
        a = feat.get("attributes") or {}
        pid, total, day = str(a.get("portid") or ""), a.get("n_total"), parse_datetime(a.get("date"))
        if pid and isinstance(total, int | float) and day:
            series.setdefault(pid, []).append((day, float(total)))
            names[pid] = clean_text(str(a.get("portname") or pid), 80)
    items = []
    for pid, points in series.items():
        points.sort()
        if len(points) < 10:
            continue
        last_day, last = points[-1]
        mean = sum(v for _, v in points[-31:-1]) / len(points[-31:-1])
        if mean > 0 and last < PORTWATCH_DROP * mean:
            items.append(Item(
                source="imf_portwatch_chokepoints", kind="logistics",
                url="https://portwatch.imf.org/pages/chokepoints",
                title=f"{names[pid]} : {last:.0f} passages le {last_day:%d/%m}, contre {mean:.0f} en moyenne",
                published_at=last_day, magnitude=round(last / mean, 3), key=f"portwatch:{pid}:{last_day:%Y-%m-%d}",
                details={"chokepoint": names[pid], "ratio": round(last / mean, 3)},
            ))
    return items


def pegel(client: httpx2.Client, station: str = "KAUB", waterway: str = "Rhin (Rhine, Rhein)") -> list[Item]:
    data = _json(client, PEGEL.format(station=quote(station, safe="")))
    value, when = data.get("value"), parse_datetime(data.get("timestamp"))
    if not isinstance(value, int | float) or value >= KAUB_LOW_WATER_CM:
        return []
    return [Item(
        source="pegelonline", kind="logistics", url="https://www.pegelonline.wsv.de/gast/start",
        title=f"Basses eaux : {station} à {value:.0f} cm", published_at=when, magnitude=float(value),
        key=f"pegel:{station}:{iso(when)}", details={"waterway": waterway, "station": station, "level_cm": value},
    )]


# --- trade policy -----------------------------------------------------------------------------------------
def federal_register(client: httpx2.Client, terms: list[str], since: datetime) -> list[Item]:
    params = {
        "conditions[term]": " | ".join(f'"{t}"' for t in terms[:6]), "per_page": "50", "order": "newest",
        "conditions[publication_date][gte]": since.strftime("%Y-%m-%d"),
        "fields[]": "title",
    }
    url = FEDERAL_REGISTER + "?fields[]=abstract&fields[]=html_url&fields[]=publication_date&fields[]=type"
    data = _json(client, url, params)
    items = []
    for d in (data or {}).get("results", []) or []:
        link = str(d.get("html_url") or "")
        items.append(Item(
            source="federal_register", kind="policy", url=link, title=clean_text(d.get("title"), 300),
            published_at=parse_datetime(d.get("publication_date")), excerpt=clean_text(d.get("abstract")),
            lang="en", domain=domain_of(link), details={"type": str(d.get("type") or "")[:40]},
        ))
    return items


def _wkt_point(text: str) -> tuple[float, float] | None:
    """(lat, lon) of « POINT (lon lat) »."""
    parts = text.replace("POINT", "").strip(" ()").split()
    try:
        lon, lat = float(parts[0]), float(parts[1])
    except (IndexError, ValueError):
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def copernicus(client: httpx2.Client, now: datetime) -> list[Item]:
    """Rapid Mapping activations of the last 30 days, newest first. The list gives each zone's centre, not its
    outline. `closed` means the mapping is done, not the event over: it is kept as a detail only."""
    data = _json(client, COPERNICUS_EMS, {"limit": "50", "offset": "0"})
    items = []
    for a in (data or {}).get("results", []) or []:
        code = str(a.get("code") or "")[:20]
        when = parse_datetime(a.get("activationTime"))
        where = _wkt_point(str(a.get("centroid") or ""))
        if not code.isalnum() or when is None or where is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if now - when > timedelta(days=COPERNICUS_DAYS):
            continue
        category = a.get("category") or {}
        slug = str(category.get("slug", "") if isinstance(category, dict) else "")[:30]
        countries = ", ".join(str(c.get("short_name", "")) for c in a.get("countries") or [] if isinstance(c, dict))
        items.append(Item(
            source="copernicus_ems", kind="hazard", url=COPERNICUS_PAGE.format(code=code),
            title=clean_text(f"{code} : {a.get('name') or ''}", 300), published_at=when, lat=where[0], lon=where[1],
            domain="mapping.emergency.copernicus.eu", key=f"cems:{code}",
            details={"event_type": COPERNICUS_TYPES.get(slug, "OT"), "category": slug, "country": countries[:80],
                     "closed": str(a.get("closed")).lower() == "true", "credit": COPERNICUS_CREDIT},
        ))
    return items


def eurlex(client: httpx2.Client, since: datetime) -> list[Item]:
    """Acts of the Official Journal, L series (RSS, 100 latest). Every act is offered to the rule filter: only those
    naming a watched material, input, site or customs code go further."""
    root = _xml(client, EURLEX_OJ_L)
    items = []
    for it in root.iter("item"):
        title = clean_text(_text(it, "title"), 400)
        celex = title.split(":", 2)[1].strip() if title.startswith("CELEX:") else ""
        if not celex.isalnum():
            continue
        published = parse_datetime(_text(it, "pubDate"))
        if published and published < since:
            continue
        url = f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"
        items.append(Item(
            source="eurlex_oj_l", kind="policy", url=url, title=title.split(":", 2)[2].strip()[:300],
            published_at=published, excerpt=clean_text(_text(it, "description")), lang="en",
            domain="eur-lex.europa.eu", key=f"eurlex:{celex}", details={"celex": celex},
        ))
    return items


# --- European media feeds ---------------------------------------------------------------------------------
MEDIA_MAX_BYTES = 2 * 1024 * 1024  # a front page feed weighs a few hundred kB
MEDIA_MAX_ITEMS = 200
ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
DC_DATE = "{http://purl.org/dc/elements/1.1/}date"


def _first(node: Any, *tags: str) -> str:
    for t in tags:
        found = node.find(t)
        if found is not None and found.text:
            return str(found.text).strip()
    return ""


def _atom_link(entry: Any) -> str:
    for link in entry.findall(ATOM + "link"):
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return str(link.get("href")).strip()
    return ""


def media_feed(client: httpx2.Client, source_id: str, url: str, lang: str, since: datetime) -> list[Item]:
    """A media feed as published (RSS 2.0, RSS 1.0 / RDF or Atom): title and summary only, the article page is never
    fetched. Unlike the search sources, nothing was selected by a query: the rule filter is stricter for these
    items (`Item.queried`, goulot/collect/rules.py)."""
    return [Item(source=source_id, kind="news", url=link, title=title, published_at=published, lang=lang[:20],
                 excerpt=summary, domain=domain_of(link), queried=False)
            for title, summary, link, published in _feed_entries(client, url, since)]


def _feed_entries(client: httpx2.Client, url: str, since: datetime, missing_ok: bool = False
                  ) -> list[tuple[str, str, str, datetime | None]]:
    """(title, summary, link, date) of a feed's entries, HTML reduced to text. `missing_ok`: a 404 is an empty feed."""
    result = fetch(client, url, max_bytes=MEDIA_MAX_BYTES)
    if result.status == 404 and missing_ok:
        return []
    if result.status != 200:
        raise SourceError(f"HTTP {result.status}")
    root = parse_xml(result.content, max_bytes=MEDIA_MAX_BYTES)
    entries = [*root.iter("item"), *root.iter(RSS1 + "item"), *root.iter(ATOM + "entry")]
    out = []
    for e in entries[:MEDIA_MAX_ITEMS]:
        link = _first(e, "link", RSS1 + "link") or _atom_link(e)
        if not link.startswith(("https://", "http://")):
            continue
        published = parse_datetime(_first(e, "pubDate", DC_DATE, ATOM + "published", ATOM + "updated"))
        if published and published < since:
            continue
        title = clean_text(_first(e, "title", RSS1 + "title", ATOM + "title"), 300)
        summary = clean_text(_first(e, "description", RSS1 + "description", ATOM + "summary"))
        out.append((title, summary, link, published))
    return out


# --- official records: regulators, emergency services, stock exchanges (pilot of 13 November 2026) ----------------
PRESSEPORTAL_CITY = "https://www.presseportal.de/rss/polizei/r/{city}.rss2"
PRESSEPORTAL_MAX_CITIES = 15
OPENDART_LIST = "https://opendart.fss.or.kr/api/list.json"
OPENDART_COMPANY = "https://opendart.fss.or.kr/api/company.json"
OPENDART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
OPENDART_MAX_PAGES = 5
OPENDART_MAX_LOOKUPS = 30
# Titles of the exchange disclosures that report a stop: Korean form name → what it says, in French.
OPENDART_TITLES = {
    "생산중단": "arrêt de production", "생산재개": "reprise de la production", "재해발생": "sinistre déclaré",
    "영업정지": "suspension d'activité", "조업중단": "arrêt des opérations", "가동중단": "arrêt d'une installation",
}


def _record(source_id: str, label: str, title: str, summary: str, link: str, published: datetime | None,
            lang: str, matched: list[Site]) -> Item:
    """An official record that names a watched site. It is a declaration, not a headline: the excerpt always says
    where it comes from; `details.sites` carries the chain's own names, which the rule filter recognizes."""
    names = sorted({s.name for s in matched})
    excerpt = f"{summary} " if summary else ""
    excerpt += f"[{label} ; site suivi : {', '.join(names)}]"
    return Item(source=source_id, kind="news", url=link, title=title, published_at=published, lang=lang[:20],
                excerpt=excerpt[:1000], domain=domain_of(link), details={"record": label, "sites": names})


def registry_feed(client: httpx2.Client, source_id: str, url: str, label: str, lang: str, since: datetime,
                  sites: list[Site], place_implied: bool = False, missing_ok: bool = False) -> list[Item]:
    """A regulator's or an emergency service's feed, reduced to the entries that name a site of the chains."""
    items = []
    for title, summary, link, published in _feed_entries(client, url, since, missing_ok):
        matched = match_sites(f"{title} {summary}", sites, place_implied)
        if matched:
            items.append(_record(source_id, label, title, summary, link, published, lang, matched))
    return items


def presseportal(client: httpx2.Client, source_id: str, since: datetime, sites: list[Site],
                 cities: dict[str, list[Site]]) -> list[Item]:
    """Police and fire brigade press releases of the German cities where the chains have a site: one feed per city,
    read against that city's sites only. A city Presseportal does not know (404) is skipped."""
    items: dict[str, Item] = {}
    for city in sorted(cities)[:PRESSEPORTAL_MAX_CITIES]:
        # « Ludwigshafen am Rhein » may be filed under « Ludwigshafen »: both are tried.
        for name in dict.fromkeys([city, city.split()[0]]):
            url = PRESSEPORTAL_CITY.format(city=quote(name, safe=""))
            for it in registry_feed(client, source_id, url, "communiqué de la police ou des pompiers, Allemagne",
                                    "de", since, cities[city], place_implied=True, missing_ok=True):
                items.setdefault(it.url, it)
    return list(items.values())


def german_cities(sites: list[Site], places: dict[str, str]) -> dict[str, list[Site]]:
    """City → sites, for the German sites whose place is known. `places`: node_key → place of the chain. The chain
    may name the city in French or in English (« Cologne »): the embedded city list gives its usual name (« Köln »),
    offline."""
    from goulot.geo import cities as geo_cities

    out: dict[str, list[Site]] = {}
    for s in sites:
        city = re.split(r"[,(/]", places.get(s.node_key, ""), maxsplit=1)[0].strip()
        if s.country_iso2 != "DE" or not city or city.lower() == "unknown" or len(city) > 40:
            continue
        known = geo_cities.exact(city, "DE") if geo_cities.available() else None
        out.setdefault(str(known["name"]) if known else city, []).append(s)
    return out


TCEQ_DETAIL = re.compile(r"^https://www2\.tceq\.texas\.gov/oce/eer/index\.cfm\?fuseaction=main\.getDetails&target=(\d+)$")
TCEQ_WINDOW = timedelta(days=8)  # the feed lists the events started in the last 7 days
TCEQ_MAX_DETAILS = 60
TCEQ_TYPES = {"EMISSIONS EVENT": "événement d'émissions", "AIR SHUTDOWN": "arrêt d'une unité",
              "AIR STARTUP": "démarrage d'une unité", "MAINTENANCE": "maintenance",
              "EXCESS OPACITY": "opacité excessive"}
# Labels of a declaration's page, in page order: each field runs from its label to the next one.
TCEQ_FIELDS = (
    ("report", "Report Type:", "Report Date:"),
    ("owner", "Name of Owner or Operator:", "CN:"),
    ("entity", "Regulated Entity Name:", "RN:"),
    ("location", "Physical Location:", "County:"),
    ("county", "County:", "Event/Activity Type:"),
    ("type", "Event/Activity Type:", "Date and Time"),
    ("start", "Scheduled Activity Start:", "Date and Time"),
    ("end", "Scheduled Activity Ended:", "Event Duration:"),
    ("duration", "Event Duration:", "Process Unit"),
    ("unit", "Process Unit or Area Common Names", "Facility Common Name"),
    ("cause", "Reason for Scheduled Activity:", "Actions Taken"),
    ("actions", "Correct the Situation:", "Basis Used"),
)
_tceq_seen: set[str] = set()  # declarations already read by this process: a page is asked once


def tceq_fields(html_page: str) -> dict[str, str]:
    """Fields of a TCEQ declaration page, as plain text. A missing label gives an empty field."""
    text = clean_text(html_page, 20_000)
    out = {}
    for name, start, end in TCEQ_FIELDS:
        m = re.search(re.escape(start) + r"\s*(.*?)\s*" + re.escape(end), text)
        out[name] = m.group(1).strip()[:600] if m else ""
    return out


TCEQ_SEARCH = "https://www2.tceq.texas.gov/oce/eer/index.cfm"
_TCEQ_ROW = re.compile(
    r"<tr[^>]*>\s*<td>\s*<a href=\"index\.cfm\?fuseaction=main\.getDetails&amp;target=(\d+)\">.*?</tr>", re.S)


def tceq_history(client: httpx2.Client, field: str, word: str, begin: datetime, end: datetime
                 ) -> tuple[list[dict[str, str]], int]:
    """Past declarations whose owner (`cust_name`) or installation (`re_name`) contains `word`, from the regulator's
    search: (rows of the first page, total announced). Read-only; for the replay of past events."""
    if field not in ("cust_name", "re_name"):
        raise ValueError("unknown search field")
    params = {"fuseaction": "main.searchresults", "newsearch": "yes", field: word, "usefullwildcard": "yes",
              "event_start_beg_dt": f"{begin:%m/%d/%Y}", "event_start_end_dt": f"{end:%m/%d/%Y}"}
    result = fetch(client, TCEQ_SEARCH, params=params, max_bytes=MEDIA_MAX_BYTES)
    if result.status != 200:
        raise SourceError(f"HTTP {result.status}")
    page = result.content.decode("latin-1")
    total = re.search(r"of (\d+) Records", page)
    rows = []
    for m in _TCEQ_ROW.finditer(page):
        cells = [clean_text(c, 200) for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(0), re.S)]
        if len(cells) >= 9:
            rows.append({"incident": m.group(1), "rn": cells[1], "entity": cells[2], "began": cells[3],
                         "ended": cells[4], "type": cells[5], "report": cells[6], "owner": cells[8]})
    return rows, int(total.group(1)) if total else len(rows)


def tceq(client: httpx2.Client, source_id: str, url: str, now: datetime, sites: list[Site]) -> list[Item]:
    """Texas regulator (TCEQ): emission events, shutdowns, start-ups and maintenance declared by the plants. The feed
    gives only the installation's name (« CHOCOLATE BAYOU PLANT »): each new declaration's page is read once for its
    owner, county, unit, cause and duration, and kept when it names a site of the chains."""
    items = []
    asked = 0
    for _title, _summary, link, published in _feed_entries(client, url, now - TCEQ_WINDOW):
        m = TCEQ_DETAIL.match(link)
        if not m or m.group(1) in _tceq_seen or asked >= TCEQ_MAX_DETAILS:
            continue
        asked += 1
        page = fetch(client, link, max_bytes=MEDIA_MAX_BYTES)
        if page.status != 200:
            continue  # asked again at the next cycle
        _tceq_seen.add(m.group(1))
        f = tceq_fields(page.content.decode("latin-1"))
        matched = match_sites(f"{f['entity']} {f['location']} {f['county']} texas", sites, owner=f["owner"])
        if not matched or not f["entity"]:
            continue
        what = TCEQ_TYPES.get(f["type"].upper(), f["type"].lower() or "déclaration")
        title = f"{f['entity']} ({f['owner']}, Texas) : {what}, incident {m.group(1)}"
        parts = [f"Unité : {f['unit']}." if f["unit"] else "",
                 f"Du {f['start']} au {f['end']} ({f['duration']})." if f["start"] else "",
                 f"Cause déclarée : {f['cause']}" if f["cause"] else "",
                 f"Mesures : {f['actions']}" if f["actions"] else "",
                 f"Rapport {f['report'].lower()}." if f["report"] else ""]
        items.append(_record(source_id, "déclaration au régulateur du Texas (TCEQ)", title[:300],
                             " ".join(p for p in parts if p)[:800], link, published, "en", matched))
    return items


def opendart(client: httpx2.Client, key: str, since: datetime, now: datetime, sites: list[Site]) -> list[Item]:
    """Korean exchange disclosures (FSS OpenDART) whose title reports a stop, a restart or a disaster, for the Korean
    companies of the chains. The company's English name is asked only for those few disclosures."""
    korean = [s for s in sites if s.country_iso2 == "KR"]
    if not korean:
        return []
    params = {"crtfc_key": key, "pblntf_ty": "I", "bgn_de": f"{since:%Y%m%d}", "end_de": f"{now:%Y%m%d}",
              "page_count": "100", "last_reprt_at": "Y"}
    rows: list[dict[str, Any]] = []
    for page in range(1, OPENDART_MAX_PAGES + 1):
        data = _json(client, OPENDART_LIST, {**params, "page_no": str(page)})
        status = str(data.get("status", "")) if isinstance(data, dict) else ""
        if status == "013":  # no data
            break
        if status != "000":
            raise SourceError(f"OpenDART {status or 'illisible'}")
        rows += [r for r in data.get("list") or [] if isinstance(r, dict)]
        if page >= int(data.get("total_page") or 1):
            break
    items, english, lookups = [], {}, 0
    for r in rows:
        report = str(r.get("report_nm") or "")
        what = next((fr for ko, fr in OPENDART_TITLES.items() if ko in report), None)
        code = str(r.get("corp_code") or "")
        if what is None or not code.isdigit():
            continue
        if code not in english and lookups < OPENDART_MAX_LOOKUPS:
            lookups += 1
            company = _json(client, OPENDART_COMPANY, {"crtfc_key": key, "corp_code": code})
            english[code] = clean_text(str(company.get("corp_name_eng") or ""), 120) if isinstance(
                company, dict) else ""
        name_en = english.get(code, "")
        name_ko = clean_text(str(r.get("corp_name") or ""), 120)
        matched = match_sites(f"{name_en} {name_ko}", korean, place_implied=True)
        rcept = str(r.get("rcept_no") or "")
        if not matched or not rcept.isdigit():
            continue
        day = parse_datetime(str(r.get("rcept_dt") or ""))
        title = f"{name_en or name_ko} : {what} (annonce à la Bourse de Corée)"
        summary = f"Annonce officielle « {clean_text(report, 120)} » déposée par {name_ko}."
        items.append(_record("opendart", "annonce boursière, Corée (DART)", title, summary,
                             OPENDART_VIEWER.format(rcept_no=rcept), day, "ko", matched))
    return items


def now_utc() -> datetime:
    return datetime.now(UTC)
