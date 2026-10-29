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
from goulot.net.transport import OutboundBlocked
from goulot.settings import REPO_ROOT, Settings

SAMPLES = REPO_ROOT / ".samples"
MAX_BYTES = 2 * 1024 * 1024
EXTRA_HOSTS = ("www.gfex.com.cn",)  # disabled in sources.yaml since 15 October: checked again here


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
    ]


def media_targets() -> list[tuple[str, str]]:
    """The European media feeds of config/sources.yaml, enabled or not: they are enabled once checked here."""
    from goulot.collect.base import load_sources

    return [(sid, str(s["url"])) for sid, s in load_sources().items() if s.get("family") == "media"]


def redirect_target(client: httpx2.Client, url: str) -> str:
    """Where a source redirects to, when the client refused to follow (host outside the allowlist): only the first
    hop, on the allowed host, is asked; the address is shown as text, to be checked before it enters the config."""
    try:
        response = client.get(url, follow_redirects=False)
    except httpx2.HTTPError:
        return ""
    location = response.headers.get("location", "") if response.is_redirect else ""
    return "".join(ch for ch in location if ch.isprintable())[:200]


def run(settings: Settings, today: date | None = None) -> list[dict[str, object]]:
    media = media_targets()
    hosts = (*EXTRA_HOSTS, *sorted({str(urlsplit(url).hostname) for _, url in media}))
    client = build_client(load_allowlist(settings.config_dir, hosts))
    SAMPLES.mkdir(exist_ok=True)
    report: list[dict[str, object]] = []
    for name, url in [*targets(today or date.today()), *media]:
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
            if isinstance(exc, OutboundBlocked):
                moved = redirect_target(client, url)
                if moved:
                    entry["redirect"] = moved
                    print(f"      redirigé vers : {moved}")
        report.append(entry)
    (SAMPLES / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return report


def portwatch_names(settings: Settings, today: date | None = None) -> tuple[list[str], list[str]]:
    """Names of the IMF PortWatch chokepoints of the last days compared with the embedded table
    (goulot/data/chokepoints.json): (in PortWatch but not in the table, in the table but not in PortWatch). The answer
    is kept in .samples/ like the others."""
    from goulot.chain.places import chokepoints
    from goulot.collect.sources import PORTWATCH

    since = (today or date.today()) - timedelta(days=10)
    client = build_client(load_allowlist(settings.config_dir))
    try:
        result = fetch(client, PORTWATCH, params={"where": f"date >= DATE '{since.isoformat()}'",
                                                  "outFields": "portid,portname", "returnGeometry": "false",
                                                  "resultRecordCount": "2000", "f": "json"}, max_bytes=MAX_BYTES)
    finally:
        client.close()
    SAMPLES.mkdir(exist_ok=True)
    (SAMPLES / "portwatch_noms.bin").write_bytes(result.content)
    found = {str(f["attributes"]["portname"]) for f in json.loads(result.content).get("features", [])}
    table = {c["portwatch_name"] for c in chokepoints().values()}
    return sorted(found - table), sorted(table - found)
