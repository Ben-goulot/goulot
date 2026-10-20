"""One function per source of `config/sources.yaml`, all through the allowlist client (CLAUDE.md, rule 6).

Formats follow the endpoints checked on 2026-10-15 (docs/DECISIONS.md). Every function returns `Item`s and
raises `httpx2.HTTPError` or `SourceError` on failure; the runner records the error type, never the URL
(keys can sit in URLs: NASA FIRMS puts it in the path).
"""

from __future__ import annotations

import csv
import io
import json
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx2

from goulot.collect.base import Item, clean_text, domain_of, iso, parse_datetime
from goulot.net.client import fetch
from goulot.net.xml import parse_xml

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

GDELT_MAX_WINDOW = timedelta(days=90)
# VIIRS on NOAA-21 and NOAA-20: Suomi NPP observations end on 2 November 2026 (FIRMS notice of October 2026).
FIRMS_PRODUCTS = ("VIIRS_NOAA21_NRT", "VIIRS_NOAA20_NRT")
# GDELT answers HTTP 429 to more than one request every 5 seconds: its queries are spaced out (a cycle runs in the
# background, a minute or two more costs nothing).
GDELT_PAUSE_S = 6.0
_gdelt_last = 0.0
KAUB_LOW_WATER_CM = 100  # below 1 m at Kaub, Rhine barges load partially (78 cm: critical)
PORTWATCH_DROP = 0.6  # a day under 60 % of the 30-day mean counts as a disruption signal
NS = {
    "georss": "http://www.georss.org/georss",
    "gdacs": "http://www.gdacs.org",
    "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#",
    "nhc": "https://www.nhc.noaa.gov",
}


class SourceError(RuntimeError):
    pass


def _json(client: httpx2.Client, url: str, params: dict[str, str] | None = None) -> Any:
    result = fetch(client, url, params=params)
    if result.status != 200:
        raise SourceError(f"HTTP {result.status}")
    try:
        return json.loads(result.content)
    except json.JSONDecodeError as exc:
        raise SourceError("invalid JSON") from exc


def _xml(client: httpx2.Client, url: str, params: dict[str, str] | None = None) -> Any:
    result = fetch(client, url, params=params)
    if result.status != 200:
        raise SourceError(f"HTTP {result.status}")
    return parse_xml(result.content)


def _text(node: Any, path: str) -> str:
    found = node.find(path, NS)
    return (found.text or "").strip() if found is not None and found.text else ""


# --- news -------------------------------------------------------------------------------------------------
def gdelt(client: httpx2.Client, query: str, since: datetime, now: datetime) -> list[Item]:
    global _gdelt_last
    start = max(since, now - GDELT_MAX_WINDOW)
    wait = _gdelt_last + GDELT_PAUSE_S - time.monotonic()
    if GDELT_PAUSE_S > 0 and wait > 0:
        time.sleep(wait)
    _gdelt_last = time.monotonic()
    data = _json(client, GDELT, {
        "query": query, "mode": "artlist", "format": "json", "maxrecords": "250", "sort": "datedesc",
        "startdatetime": start.strftime("%Y%m%d%H%M%S"), "enddatetime": now.strftime("%Y%m%d%H%M%S"),
    })
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
        items.append(Item(
            source="gdacs", kind="hazard", url=link, title=clean_text(_text(it, "title"), 300),
            published_at=parse_datetime(_text(it, "pubDate")), excerpt=clean_text(_text(it, "description")),
            domain=domain_of(link), lat=lat, lon=lon, key="gdacs:" + (_text(it, "guid") or link),
            details={"event_type": _text(it, "gdacs:eventtype")[:10], "alert_level": _text(it, "gdacs:alertlevel")[:10],
                     "country": _text(it, "gdacs:country")[:80]},
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
    """Active fires near located sites. `boxes`: (node_key, lat, lon); one request per box of ±0.5° and per
    satellite. A fire seen by both satellites the same day, within about 1 km, is kept once."""
    items = []
    seen: set[str] = set()
    for node_key, lat, lon in boxes:
        area = f"{lon - 0.5:.3f},{lat - 0.5:.3f},{lon + 0.5:.3f},{lat + 0.5:.3f}"
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
    data = _json(client, PORTWATCH, {
        "where": f"date >= DATE '{since}'", "outFields": "date,portid,portname,n_total", "orderByFields": "date",
        "resultRecordCount": "2000", "f": "json",
    })
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


def now_utc() -> datetime:
    return datetime.now(UTC)
