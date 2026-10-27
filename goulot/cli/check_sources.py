"""`goulot dev check-sources`: from the user's Mac, ask each source that Claude Code cannot reach from its sandbox,
through the app's own client (allowlist, HTTPS, DNS checks), and keep what it answers in `.samples/` (git-ignored).

Development only. The answers are public pages and data files; they are saved as files, never executed, and read
later as data to write the readers. No key is sent: every address below is public.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from urllib.parse import urlsplit

import httpx2

from goulot.net.allowlist import load_allowlist
from goulot.net.client import build_client, fetch
from goulot.settings import REPO_ROOT, Settings

SAMPLES = REPO_ROOT / ".samples"
MAX_BYTES = 2 * 1024 * 1024
# Hosts asked here only, not by the app: www.gfex.com.cn (disabled in sources.yaml since 15 October) and the three
# candidate sources of 27 October 2026 (REACH, Copernicus EMS, EUR-Lex), approved by the user for this trial.
EXTRA_HOSTS = ("www.gfex.com.cn", "echa.europa.eu", "mapping.emergency.copernicus.eu", "eur-lex.europa.eu")


def _last_weekday(today: date) -> date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def targets(today: date) -> list[tuple[str, str]]:
    """(name, URL): home pages, then the daily data files whose address is published by each exchange."""
    day = _last_weekday(today)
    ymd = day.strftime("%Y%m%d")
    return [
        ("dce_accueil", "https://www.dce.com.cn/"),
        ("dce_cotations", "https://www.dce.com.cn/publicweb/quotesdata/dayQuotesCh.html"),
        ("czce_accueil", "https://www.czce.com.cn/"),
        ("czce_quotidien", f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/{day.year}/{ymd}/FutureDataDaily.txt"),
        ("gfex_accueil", "https://www.gfex.com.cn/"),
        ("shfe_quotidien", f"https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{ymd}.dat"),
        ("nbs_liste", "https://www.stats.gov.cn/sj/zxfb/"),
        ("ecics_acrylonitrile", "https://ec.europa.eu/taxation_customs/dds2/ecics/chemicalsubstance_list.jsp"
                                "?Lang=en&offset=0&Cas=107-13-1&sortOrder=1"),
        # Candidate sources (27 October 2026): automated access, formats and terms, read from the user's Mac.
        ("echa_liste_candidate", "https://echa.europa.eu/candidate-list-table"),
        ("echa_annexe_xiv", "https://echa.europa.eu/authorisation-list"),
        ("echa_annexe_xvii", "https://echa.europa.eu/substances-restricted-under-reach"),
        ("echa_intentions", "https://echa.europa.eu/registry-of-restriction-intentions"),
        ("echa_mentions_legales", "https://echa.europa.eu/legal-notice"),
        ("cems_activations", "https://mapping.emergency.copernicus.eu/activations/api/activations/?limit=5"),
        ("cems_api_doc", "https://mapping.emergency.copernicus.eu/api/redoc/"),
        ("cems_citation", "https://mapping.emergency.copernicus.eu/about/citations-guidelines/"),
        ("eurlex_flux_jo_l", "https://eur-lex.europa.eu/EN/display-feed.rss?rssId=165"),
        ("eurlex_reutilisation", "https://eur-lex.europa.eu/content/help/data-reuse/reuse-contents-eurlex-details.html"),
        ("eurlex_mentions_legales", "https://eur-lex.europa.eu/content/legal-notice/legal-notice.html"),
    ]


def run(settings: Settings, today: date | None = None) -> list[dict[str, object]]:
    client = build_client(load_allowlist(settings.config_dir, EXTRA_HOSTS))
    SAMPLES.mkdir(exist_ok=True)
    report: list[dict[str, object]] = []
    for name, url in targets(today or date.today()):
        started = time.monotonic()
        entry: dict[str, object] = {"name": name, "url": url}
        try:
            result = fetch(client, url, max_bytes=MAX_BYTES)
            body = result.content
            path = SAMPLES / f"{name}_{hashlib.sha256(url.encode()).hexdigest()[:8]}.bin"
            path.write_bytes(body)
            entry.update(status=result.status, bytes=len(body), file=path.name)
            print(f"  {'✅' if result.status == 200 else '⚠️ '} {name} : HTTP {result.status}, {len(body)} octets "
                  f"({time.monotonic() - started:.0f} s)")
        except (httpx2.HTTPError, ValueError) as exc:
            entry.update(error=f"{type(exc).__name__}: {str(exc)[:160]}")
            print(f"  ❌ {name} ({urlsplit(url).hostname}) : {type(exc).__name__}, {str(exc)[:100]}")
        report.append(entry)
    (SAMPLES / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return report


def portwatch_names(settings: Settings) -> tuple[list[str], list[str]]:
    """Names of the IMF PortWatch chokepoints compared with the embedded table (goulot/data/chokepoints.json):
    (in PortWatch but not in the table, in the table but not in PortWatch)."""
    from goulot.chain.places import chokepoints
    from goulot.collect.sources import PORTWATCH

    client = build_client(load_allowlist(settings.config_dir))
    try:
        result = fetch(client, PORTWATCH, params={"where": "1=1", "outFields": "portname",
                                                  "returnDistinctValues": "true", "returnGeometry": "false",
                                                  "f": "json"})
    finally:
        client.close()
    found = {str(f["attributes"]["portname"]) for f in json.loads(result.content).get("features", [])}
    table = {c["portwatch_name"] for c in chokepoints().values()}
    return sorted(found - table), sorted(table - found)
