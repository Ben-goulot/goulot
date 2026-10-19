"""Leads for the case research: past events from public databases that touch a country of the chain type
(docs/SPEC.md, section 6). Never from Ma chaîne.

GDACS (archive search API): orange and red alerts (earthquakes, cyclones, floods, volcanoes, droughts), one query
per year. eMARS (Seveso accidents) has no API and ReliefWeb needs an approved application name: not wired.
A lead is data, never an instruction: only its date, its countries and its title (folded to plain text) reach the
research call, and its address is built by the code from its numeric identifier.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx2

from goulot.collect.base import clean_text
from goulot.net.client import fetch

log = logging.getLogger("goulot.history.leads")

SEARCH = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
REPORT = "https://www.gdacs.org/report.aspx?eventid={id}&eventtype={type}"
TYPES = ("EQ", "TC", "FL", "VO", "DR")
MAX_LEADS = 20
MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Lead:
    ref: str
    day: date
    countries: tuple[str, ...]  # ISO2
    title: str
    alert: str
    url: str

    def as_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "day": self.day.isoformat(), "countries": list(self.countries), "title": self.title,
                "alert": self.alert, "url": self.url}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Lead:
        return cls(str(d["ref"]), date.fromisoformat(str(d["day"])), tuple(str(c) for c in d["countries"]),
                   str(d["title"]), str(d["alert"]), str(d["url"]))

    def line(self) -> str:
        return f"{self.ref} | {self.day.isoformat()} | {', '.join(self.countries)} | {self.title} | GDACS {self.alert}"


def parse(text: str, countries: set[str]) -> list[tuple[date, str, tuple[str, ...], str, str]]:
    """(day, alert, countries, title, url) of the events touching one of `countries`."""
    if not text.strip():
        return []
    out = []
    for feature in json.loads(text).get("features") or []:
        p = feature.get("properties") or {}
        touched = tuple(sorted({str(c.get("iso2", "")).upper() for c in p.get("affectedcountries") or []
                                if isinstance(c, dict)} & countries))
        event_type, event_id = str(p.get("eventtype", "")), p.get("eventid")
        if not touched or event_type not in TYPES or not isinstance(event_id, int):
            continue
        try:
            day = date.fromisoformat(str(p.get("fromdate", ""))[:10])
        except ValueError:
            continue
        out.append((day, str(p.get("alertlevel", "")), touched, clean_text(str(p.get("name", "")), 120),
                    REPORT.format(id=event_id, type=event_type)))
    return out


def gdacs(client: httpx2.Client, countries: set[str], since: date, today: date) -> list[Lead]:
    """Red alerts first, then orange, newest first; at most 20."""
    found: dict[str, tuple[date, str, tuple[str, ...], str, str]] = {}
    for year in range(since.year, today.year + 1):
        params = {"eventlist": ";".join(TYPES), "fromDate": f"{year}-01-01", "toDate": f"{year}-12-31",
                  "alertlevel": "Orange;Red"}
        try:
            result = fetch(client, SEARCH, params=params, max_bytes=MAX_BYTES)
        except httpx2.HTTPError as exc:
            log.info("gdacs archive unavailable: %s", type(exc).__name__)
            continue
        if result.status not in (200, 204):
            continue
        try:
            rows = parse(result.content.decode("utf-8", errors="replace"), countries)
        except (json.JSONDecodeError, AttributeError):
            continue
        for row in rows:
            if row[0] >= since:
                found.setdefault(row[4], row)
    ranked = sorted(found.values(), key=lambda r: (r[1] != "Red", -r[0].toordinal()))[:MAX_LEADS]
    ranked.sort(key=lambda r: r[0])
    return [Lead(f"lead{i}", day, touched, title, alert, url)
            for i, (day, alert, touched, title, url) in enumerate(ranked, 1)]
