"""`goulot dev check-sources`: from the user's Mac, ask each source that Claude Code cannot reach from its sandbox,
through the app's own client (allowlist, HTTPS, DNS checks), and keep what it answers in `.samples/` (git-ignored).

Development only. The answers are public pages and data files; they are saved as files, never executed, and read
later as data to write the readers. No key is sent: every address below is public.
"""

from __future__ import annotations

import hashlib
import json
import re
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
# The USGS world table is about 3 MB: it gets the same cap as the reader of the cross-check (USGS_MAX_BYTES).
LARGE_TARGETS = {"usgs_mcs_2026_csv": 16 * 1024 * 1024}
USGS_FILE = "https://www.sciencebase.gov/catalog/file/get/69837e43b66b01367d7ec7c7?f=__disk__"
# gfex: disabled since 15 October; sciencebase: USGS MCS; the last three: official records, disabled until checked.
EXTRA_HOSTS = ("www.gfex.com.cn", "www.sciencebase.gov", "www2.tceq.texas.gov", "www.presseportal.de",
               "opendart.fss.or.kr")


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
        # The Economic Times lists its feeds there: the metals and mining one answered empty on 6 November 2026.
        ("et_liste_flux", "https://economictimes.indiatimes.com/rss.cms"),
        # USGS Mineral Commodity Summaries 2026 data release (doi 10.5066/P1WKQ63T): its file list gives the address
        # of the world production table, to write in `data_url` of `usgs_mcs` (11 November 2026).
        ("usgs_mcs_2026", "https://www.sciencebase.gov/catalog/item/69837e43b66b01367d7ec7c7?format=json"),
        # Its two files, listed by that record on 12 November 2026: the column descriptions (FGDC XML, 13 kB),
        # and the table itself (CSV, 3.1 MB), read to write the reader of the cross-check.
        ("usgs_mcs_2026_xml", USGS_FILE + "59%2F9f%2F78%2F599f78dcdae6cfc820ec8410e8a0e684c28be9d2"),
        ("usgs_mcs_2026_csv", USGS_FILE + "d3%2Fac%2F84%2Fd3ac8466552946c5e8caa2c2c6338d9e1aff655d"),
        # The search the app runs each week for the next edition (goulot/chain/usgs_table.py), on this year's.
        ("usgs_mcs_search", "https://www.sciencebase.gov/catalog/items?q=Mineral+Commodity+Summaries+2026+Data+Release"
                            "&format=json&max=20&fields=title,files"),
        # Official records (pilot of 13 November 2026): the Texas regulator's hourly feed and its search form (for
        # the replay of past events), and a German city's police and fire brigade feed.
        ("tceq_flux", "https://www2.tceq.texas.gov/oce/eer/RSSDataFeed.xml"),
        ("tceq_recherche", "https://www2.tceq.texas.gov/oce/eer/index.cfm"),
        # One declaration's own page (owner, unit, cause, duration), to write its reader.
        ("tceq_fiche", "https://www2.tceq.texas.gov/oce/eer/index.cfm?fuseaction=main.getDetails&target=468147"),
        # Its search, asked by address like the form does: a year of declarations of one owner of the test chain.
        ("tceq_histo", "https://www2.tceq.texas.gov/oce/eer/index.cfm?fuseaction=main.searchresults&newsearch=yes"
                       "&cust_name=ASCEND&usefullwildcard=yes&event_start_beg_dt=11/01/2025"
                       "&event_start_end_dt=11/14/2026"),
        ("presseportal_leverkusen", "https://www.presseportal.de/rss/polizei/r/Leverkusen.rss2"),
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


def run(settings: Settings, today: date | None = None, only: str | None = None) -> list[dict[str, object]]:
    media = media_targets()
    hosts = (*EXTRA_HOSTS, *sorted({str(urlsplit(url).hostname) for _, url in media}))
    client = build_client(load_allowlist(settings.config_dir, hosts))
    SAMPLES.mkdir(exist_ok=True)
    report: list[dict[str, object]] = []
    wanted = [(n, u) for n, u in [*targets(today or date.today()), *media] if not only or only in n]
    for name, url in wanted:
        started = time.monotonic()
        entry: dict[str, object] = {"name": name, "url": url}
        try:
            result = fetch(client, url, max_bytes=LARGE_TARGETS.get(name, MAX_BYTES))
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


def registry_probe(settings: Settings, client: httpx2.Client | None = None) -> list[str]:
    """What the three record sources would bring today: entries read over 30 days, and those that name a site of the
    typical chains (titles of public records only; counts, never the chains themselves). Lines to print."""
    from collections.abc import Callable
    from datetime import UTC, datetime, timedelta

    from goulot.collect import sitematch, sources
    from goulot.collect.base import Item, load_sources
    from goulot.db.connection import Database
    from goulot.security.secrets import get_secret

    if not settings.db_path.exists():
        return ["  Registres : base absente, rien à comparer."]
    now = datetime.now(UTC)
    since = now - timedelta(days=30)
    with Database(settings.db_path).connect() as conn:
        watched = sitematch.watch_sites(conn)
        places = {r["node_key"]: r["place"] for r in conn.execute(
            "SELECT node_key, place FROM chain_nodes WHERE valid_to IS NULL")}
    by_country: dict[str, int] = {}
    for s in watched:
        by_country[s.country_iso2] = by_country.get(s.country_iso2, 0) + 1
    lines = [f"  Registres : {len(watched)} sites producteurs dans les chaînes types, dont {by_country.get('US', 0)} "
             f"aux États-Unis, {by_country.get('DE', 0)} en Allemagne, {by_country.get('KR', 0)} en Corée."]
    own = client is None
    client = client or build_client(load_allowlist(settings.config_dir, EXTRA_HOSTS))

    def show(name: str, read: Callable[[], list[Item]], total: int | None = None) -> None:
        try:
            items = read()
        except (httpx2.HTTPError, ValueError, sources.SourceError) as exc:
            lines.append(f"  ❌ {name} : {type(exc).__name__}, {str(exc)[:120]}")
            return
        seen = "" if total is None else f"{total} entrées lues, "
        lines.append(f"  ✅ {name} : {seen}{len(items)} nomme(nt) un site des chaînes")
        lines.extend(f"      · {it.published_at:%d/%m} {it.title[:110]}" if it.published_at else
                     f"      · {it.title[:110]}" for it in items[:8])

    try:
        tceq_url = str(load_sources()["tceq_emissions"]["url"])
        try:
            entries = len(sources._feed_entries(client, tceq_url, since))
        except (httpx2.HTTPError, ValueError, sources.SourceError):
            entries = None
        show("Texas (TCEQ)", lambda: sources.tceq(
            client, "tceq_emissions", tceq_url, now, [s for s in watched if s.country_iso2 == "US"]), entries)
        cities = sources.german_cities(watched, places)
        show(f"Allemagne (Presseportal, {len(cities)} ville(s) : {', '.join(sorted(cities)[:8])})",
             lambda: sources.presseportal(client, "presseportal_blaulicht", since, watched, cities))
        key = get_secret("opendart")
        if key:
            show("Corée (OpenDART)", lambda: sources.opendart(client, key, since, now, watched))
        else:
            lines.append("  ⚪ Corée (OpenDART) : pas de clé dans le trousseau, non essayée")
    finally:
        if own:
            client.close()
    return lines


def records_history(settings: Settings, months: int = 12, client: httpx2.Client | None = None,
                    begin_day: str | None = None, end_day: str | None = None) -> list[str]:
    """What the Texas regulator recorded over the past months for the American sites of the typical chains: one
    search by owner and one by installation on the first distinctive word of each site, kept when the row names the
    site. Read-only, without AI. Lines to print."""
    from datetime import UTC, datetime, timedelta

    from goulot.collect import sitematch, sources
    from goulot.db.connection import Database

    if not settings.db_path.exists():
        return ["Base absente : rien à relever."]
    end = datetime.now(UTC)
    begin = end - timedelta(days=30 * months)
    if begin_day and end_day:  # a past crisis replayed: « 2021-02-10 » to « 2021-03-10 »
        begin = datetime.fromisoformat(begin_day).replace(tzinfo=UTC)
        end = datetime.fromisoformat(end_day).replace(tzinfo=UTC)
    with Database(settings.db_path).connect() as conn:
        watched = [s for s in sitematch.watch_sites(conn) if s.country_iso2 == "US"]
    own = client is None
    client = client or build_client(load_allowlist(settings.config_dir, EXTRA_HOSTS))
    lines = [f"Déclarations au régulateur du Texas (TCEQ) du {begin:%d/%m/%Y} au {end:%d/%m/%Y}, "
             f"pour {len(watched)} sites américains des chaînes types :"]

    def hours(row: dict[str, str]) -> float:
        try:
            a = datetime.strptime(row["began"], "%m/%d/%Y %I:%M %p")
            b = datetime.strptime(row["ended"], "%m/%d/%Y %I:%M %p")
        except ValueError:
            return 0.0
        return max((b - a).total_seconds() / 3600, 0.0)

    try:
        for site in watched:
            found: dict[str, dict[str, str]] = {}
            others: set[str] = set()
            truncated = False
            try:
                for field in ("cust_name", "re_name"):
                    rows, total = sources.tceq_history(client, field, site.lead.upper(), begin, end)
                    truncated = truncated or total > len(rows)
                    for r in rows:
                        owner = re.sub(r"^CN\d+\s*", "", r["owner"])  # « CN604255158 ASCEND … »
                        if sitematch.names_site(f"{r['entity']} texas", site, owner=owner):
                            found[r["incident"]] = r
                        else:
                            others.add(r["entity"])
            except (httpx2.HTTPError, ValueError, sources.SourceError) as exc:
                lines.append(f"  ❌ {site.name} : {type(exc).__name__}, {str(exc)[:100]}")
                continue
            kinds: dict[str, int] = {}
            for r in found.values():
                kinds[r["type"]] = kinds.get(r["type"], 0) + 1
            long_ones = sorted(found.values(), key=hours, reverse=True)[:3]
            detail = ", ".join(f"{n} {k.lower()}" for k, n in sorted(kinds.items())) or "aucune"
            lines.append(f"  {'✅' if found else '⚪'} {site.name} : {len(found)} déclaration(s) ({detail})"
                         + (" ; liste tronquée à la première page" if truncated else ""))
            lines.extend(f"      · {r['began']} : {r['type'].lower()}, {hours(r):.0f} h, {r['entity'][:60]}"
                         for r in long_ones)
            if not found and others:
                lines.append(f"      installations au nom proche, non retenues : {', '.join(sorted(others)[:6])}")
    finally:
        if own:
            client.close()
    return lines


GOOGLE_PROBE_QUERIES = ("acrylonitrile", "gallium", "lithium carbonate")  # names of the 3 test CAS only
RELIABLE = 0.7


def google_news_probe(settings: Settings, client: httpx2.Client | None = None) -> dict[str, object]:
    """Decision of 4 November 2026 (full text before the triage): what the Google News items carry, from which
    publishers, and whether an article link leads to the publisher's page by a plain HTTP redirect. Only the names
    of the 3 test materials are searched; the link is asked once, without following it."""
    from goulot.collect import sources
    from goulot.collect.rules import headline_only
    from goulot.pipeline.corroborate import domain_reliability

    own = client is None
    client = client or build_client(load_allowlist(settings.config_dir))
    try:
        items = [it for q in GOOGLE_PROBE_QUERIES for it in sources.google_news(client, f"{q} when:7d")]
        publishers = [it.provider_domain or it.domain for it in items]
        reliable = sorted({p for p in publishers if domain_reliability(p) >= RELIABLE})
        title_only = sum(1 for it in items if headline_only(it.title, it.excerpt))
        out: dict[str, object] = {"items": len(items), "title_only": title_only,
                                  "reliable_items": sum(1 for p in publishers if domain_reliability(p) >= RELIABLE),
                                  "reliable_publishers": reliable, "link": None}
        first = next((it for it in items if it.url.startswith("https://news.google.com/")), None)
        if first is not None:
            response = client.get(first.url, follow_redirects=False)
            location = response.headers.get("location", "") if response.is_redirect else ""
            SAMPLES.mkdir(exist_ok=True)
            (SAMPLES / "google_news_article_link.bin").write_bytes(response.content[:MAX_BYTES])
            body = response.content[:MAX_BYTES].decode("utf-8", "replace")
            out["link"] = {"status": response.status_code, "redirect_host": urlsplit(location).hostname or "",
                           "needs_javascript": "data-n-a-sg" in body or "c-wiz" in body}
        return out
    finally:
        if own:
            client.close()


PORTWATCH_DAYS = 30  # PortWatch publishes about two weeks late (25 October on 6 November 2026)


def portwatch_names(settings: Settings, today: date | None = None) -> tuple[list[str], list[str], str]:
    """Names of the IMF PortWatch chokepoints of the last 30 days compared with the embedded table
    (goulot/data/chokepoints.json): (in PortWatch but not in the table, in the table but not in PortWatch, latest
    day published). The answer is kept in .samples/ like the others."""
    from goulot.chain.places import chokepoints
    from goulot.collect.sources import PORTWATCH, SourceError, arcgis_query

    since = (today or date.today()) - timedelta(days=PORTWATCH_DAYS)
    client = build_client(load_allowlist(settings.config_dir))
    try:
        answer = arcgis_query(client, PORTWATCH, {"where": f"date >= DATE '{since.isoformat()}'",
                                                  "outFields": "date,portid,portname", "returnGeometry": "false",
                                                  "orderByFields": "date", "resultRecordCount": "2000", "f": "json"})
    except SourceError as exc:  # ArcGIS reports a refused query in a JSON body
        raise ValueError(f"PortWatch refused the query: {str(exc)[:160]}") from exc
    finally:
        client.close()
    SAMPLES.mkdir(exist_ok=True)
    (SAMPLES / "portwatch_noms.bin").write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
    features = [f.get("attributes") or {} for f in answer.get("features", [])]
    found = {str(a["portname"]) for a in features if a.get("portname")}
    if not found:
        raise ValueError(f"PortWatch answered no transit for the last {PORTWATCH_DAYS} days")
    latest = max((str(a.get("date") or "")[:10] for a in features), default="")
    table = {c["portwatch_name"] for c in chokepoints().values()}
    return sorted(found - table), sorted(table - found), latest
