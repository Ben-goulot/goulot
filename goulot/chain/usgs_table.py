"""USGS world production table of the chain cross-check, kept up to date without anyone (docs/SPEC.md, section 3).

Each February the USGS publishes a new Mineral Commodity Summaries data release, as a new ScienceBase record with a
new file address. Once a week, while the table in use is from an earlier year, the app searches ScienceBase for the
current year's release, takes its `MCS<year>_Commodities_Data.csv`, checks that it reads (world production of
lithium or gallium found) and only then adopts it (setting `usgs_mcs_table`). Anything else keeps the table in use
and records why; `goulot doctor` shows it. No AI, no key; through the app's client (allowlist: www.sciencebase.gov).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2

from goulot.chain import crosscheck
from goulot.db.connection import get_setting, set_setting
from goulot.net.client import fetch

log = logging.getLogger("goulot.chain.usgs")

SETTING = "usgs_mcs_table"
SEARCH_URL = "https://www.sciencebase.gov/catalog/items"
FILE_PREFIX = "https://www.sciencebase.gov/catalog/file/get/"
TITLE = re.compile(r"^Mineral Commodity Summaries (\d{4}) Data Release\b")
CHECK_EVERY = timedelta(days=7)
SEARCH_MAX_BYTES = 2 * 1024 * 1024
LATE_AFTER_MONTH = 4  # the release comes out in late January or February: from May, a missing one is worth a look
PROBES = ("lithium", "gallium")


def configured() -> dict[str, Any]:
    """Table shipped in config/sources.yaml (`data_url`, `data_year` of `usgs_mcs`)."""
    from goulot.collect.base import load_sources

    source = load_sources().get("usgs_mcs", {})
    if not source.get("enabled"):
        return {"url": "", "year": None}
    year = source.get("data_year")
    return {"url": str(source.get("data_url") or ""), "year": int(year) if year else None}


def state(conn: sqlite3.Connection) -> dict[str, Any]:
    """Table in use: the one adopted by the app if any, else the configured one; with the last check."""
    saved = json.loads(get_setting(conn, SETTING) or "{}")
    base = configured()
    if saved.get("url") and (base["year"] is None or int(saved.get("year") or 0) >= base["year"]):
        table = {"url": saved["url"], "year": saved.get("year"), "source": "app"}
    else:  # a newer table shipped with a version wins over an older one adopted earlier
        table = {"url": base["url"], "year": base["year"], "source": "config"}
    return {**table, "checked_at": saved.get("checked_at"), "error": saved.get("error", ""),
            "adopted_at": saved.get("adopted_at")}


def url_in_use(conn: sqlite3.Connection | None) -> str:
    if conn is None:
        return str(configured()["url"])
    return str(state(conn)["url"] or "")


def due(conn: sqlite3.Connection, now: datetime) -> bool:
    s = state(conn)
    if s["year"] is not None and int(s["year"]) >= now.year:
        return False  # this year's table is in use: nothing to look for until next January
    if not s["checked_at"]:
        return True
    return now - datetime.fromisoformat(s["checked_at"]) >= CHECK_EVERY


def find_release(client: httpx2.Client, year: int) -> str | None:
    """Address of `MCS<year>_Commodities_Data.csv` in the ScienceBase record of that year's release, or None."""
    params = {"q": f"Mineral Commodity Summaries {year} Data Release", "format": "json", "max": "20",
              "fields": "title,files"}
    result = fetch(client, SEARCH_URL, params=params, max_bytes=SEARCH_MAX_BYTES)
    if result.status != 200:
        raise ValueError(f"recherche ScienceBase : HTTP {result.status}")
    try:
        items = json.loads(result.content).get("items") or []
    except (json.JSONDecodeError, AttributeError) as exc:
        raise ValueError("recherche ScienceBase : réponse illisible") from exc
    wanted = f"MCS{year}_Commodities_Data.csv"
    for item in items:
        title = TITLE.match(str(item.get("title") or ""))
        if not title or int(title.group(1)) != year:
            continue
        for f in item.get("files") or []:
            url = str(f.get("url") or f.get("downloadUri") or "")
            if str(f.get("name") or "") == wanted and url.startswith(FILE_PREFIX):
                return url
    return None


def readable(client: httpx2.Client, url: str) -> bool:
    """The table reads: world production shares of at least one known commodity."""
    text = crosscheck.fetch_usgs_csv(client, url)
    return bool(text) and any(crosscheck.usgs_production_shares(text or "", c, url) for c in PROBES)


def check(conn: sqlite3.Connection, client: httpx2.Client, now: datetime) -> str:
    """One weekly check. Returns « adopted », « not_published », « unreadable » or « failed » (French reason kept)."""
    saved = json.loads(get_setting(conn, SETTING) or "{}")
    year = now.year
    outcome, error = "not_published", ""
    try:
        url = find_release(client, year)
        if url is None:
            error = f"édition {year} pas encore publiée sur ScienceBase"
        elif not readable(client, url):
            outcome, error = "unreadable", f"édition {year} trouvée mais illisible : table gardée"
        else:
            outcome = "adopted"
            saved.update({"url": url, "year": year, "adopted_at": now.isoformat(timespec="seconds")})
            log.info("usgs table adopted: year=%s", year)
    except (httpx2.HTTPError, ValueError) as exc:
        outcome, error = "failed", str(exc)[:200] if isinstance(exc, ValueError) else type(exc).__name__
    saved.update({"checked_at": now.isoformat(timespec="seconds"), "error": error})
    set_setting(conn, SETTING, json.dumps(saved, ensure_ascii=False))
    if error:
        log.info("usgs table check: %s", outcome)
    return outcome


def tick(ctx: Any) -> str | None:
    """Scheduler entry point (every 10 minutes): a check only when one is due."""
    now = datetime.fromtimestamp(ctx.clock.time(), UTC)
    with ctx.db.connect() as conn:
        if not due(conn, now):
            return None
    http = ctx.http_client()
    try:
        with ctx.db.transaction() as conn:
            return check(conn, http, now)
    except Exception:
        log.exception("usgs table check crashed")
        return None
    finally:
        http.close()


def doctor_line(conn: sqlite3.Connection, now: datetime) -> tuple[bool, str]:
    """(fine, French text) for `goulot doctor`."""
    s = state(conn)
    if not s["url"]:
        return False, "aucune table configurée : recoupement USGS sauté"
    text = f"édition {s['year'] or '?'}" + (" (adoptée automatiquement)" if s["source"] == "app" else "")
    if s["checked_at"]:
        text += f", vérifiée le {s['checked_at'][:10]}"
    late = s["year"] is not None and int(s["year"]) < now.year and now.month >= LATE_AFTER_MONTH
    if s["error"]:
        text += f" ; {s['error']}"
    return not late, text
