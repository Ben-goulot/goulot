"""Texas regulator (TCEQ): emission events, shutdowns, start-ups and maintenance declared by the plants, within 24
hours by law (docs/SPEC.md, section 4, « Registres »). The trade press writes from these filings.

Three things are read, all public, without a key:
- the hourly feed of the declarations of the last 7 days: number, installation number (RN…), installation name;
- a declaration's own page: owner, county, unit, cause, substances, start, end, when the regulator was told,
  first or final report;
- the search form, by owner, county or installation number: at most 50 lines an answer, without pages, so a long
  period is asked year by year.

What is done with them is in `records.py` (store, habit of each installation, what is worth sending) and in
`identity.py` (who operates a site). Requests are counted and capped per cycle: the catch-up of a new site (who it
is, which installation, its past five years) is spread over a few cycles.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx2

from goulot.collect import identity, records
from goulot.collect.base import Item, clean_text, fold, iso
from goulot.collect.records import Declaration
from goulot.collect.sitematch import Site, match_sites
from goulot.collect.sources import SourceError, _feed_entries, _record
from goulot.db.connection import Database
from goulot.net.client import fetch

log = logging.getLogger("goulot.collect")
SEARCH = "https://www2.tceq.texas.gov/oce/eer/index.cfm"
DETAIL = SEARCH + "?fuseaction=main.getDetails&target={ext_id}"
LABEL = "déclaration au régulateur du Texas (TCEQ)"
TEXAS_TIME = ZoneInfo("America/Chicago")
MAX_BYTES = 3_000_000
FEED_WINDOW = timedelta(days=8)  # the feed lists the events started in the last 7 days
SEARCH_CAP = 50  # lines of an answer of the search form; it has no second page
HISTORY_YEARS = records.PROFILE_YEARS
REREAD_EVERY = timedelta(hours=20)
REREAD_FOR = timedelta(days=45)
RECHECK_NONE = timedelta(days=30)
RETRY_PAUSE = 4.0  # seconds before the one second try of a search
PACE = 0.0  # seconds after each search; the diagnostic, which asks hundreds at once, sets it
PAGES_PROOF = 3  # so many pages asked and none read: the page changed, or the site is down
TYPES = {"EMISSIONS EVENT": "incident avec émissions", "AIR SHUTDOWN": "arrêt programmé d'une unité",
         "AIR STARTUP": "démarrage d'une unité", "MAINTENANCE": "maintenance", "EXCESS OPACITY": "fumées anormales"}
_TITLE = re.compile(r"^(\d+)\s*-\s*(RN\d+)\s*(.*)$")
_ROW = re.compile(
    r"<tr[^>]*>\s*<td>\s*<a href=\"index\.cfm\?fuseaction=main\.getDetails&amp;target=(\d+)\">.*?</tr>", re.S)
# Labels of a declaration's page, in page order: each field runs from its label to the next one.
FIELDS = (
    ("status", "Incident Status:", "Report Type:"),
    ("report", "Report Type:", "Report Date:"),
    ("owner", "Name of Owner or Operator:", "CN:"),
    ("entity", "Regulated Entity Name:", "RN:"),
    ("ref", "RN:", "Physical Location:"),
    ("location", "Physical Location:", "County:"),
    ("county", "County:", "Event/Activity Type:"),
    ("type", "Event/Activity Type:", "Date and Time"),
    ("start", "Scheduled Activity Start:", "Date and Time"),
    ("end", "Scheduled Activity Ended:", "Event Duration:"),
    ("unit", "Process Unit or Area Common Names", "Facility Common Name"),
    ("compounds", "List of Air Contaminant Compounds", "Cause of Emission Event"),
    ("cause", "Reason for Scheduled Activity:", "Actions Taken"),
    ("notified", "Initial Notification Date/Time:", "Method:"),
)


@dataclass
class Budget:
    """Requests one cycle may spend on each kind of work."""

    pages: int = 40
    identities: int = 2
    sites: int = 2
    backfills: int = 2


class TceqError(SourceError):
    pass


def when(text: str) -> datetime | None:
    """« 02/15/2021 01:56 AM », Texas time."""
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y %I:%M %p").replace(tzinfo=TEXAS_TIME).astimezone(UTC)
    except ValueError:
        return None


def parse_page(source: str, ext_id: str, html_page: str) -> Declaration | None:
    """A declaration's own page. None when the page is not a declaration (no installation named)."""
    text = clean_text(html_page, 30_000)
    f = {}
    for name, start, end in FIELDS:
        m = re.search(re.escape(start) + r"\s*(.*?)\s*" + re.escape(end), text)
        f[name] = m.group(1).strip()[:600] if m else ""
    if not f["entity"]:
        return None
    # « - 8 total Description Est. Quantity/Opacity Units … Authorization Ammonia 287636.0 POUNDS … »
    compounds = re.sub(r"^.*?Authorization\s*", "", f["compounds"])
    return Declaration(
        source, ext_id, ref=f["ref"] if re.fullmatch(r"RN\d+", f["ref"]) else "", entity=f["entity"],
        owner=f["owner"], county=f"{f['county']}".upper(), kind=f["type"].upper(), unit=f["unit"], cause=f["cause"],
        compounds=compounds, started_at=when(f["start"]), ended_at=when(f["end"]), notified_at=when(f["notified"]),
        final=f["report"].upper() == "FINAL" or f["status"].upper() == "CLOSED", detailed=True,
        url=DETAIL.format(ext_id=ext_id))


def page(client: httpx2.Client, source: str, ext_id: str) -> Declaration | None:
    result = fetch(client, DETAIL.format(ext_id=ext_id), max_bytes=MAX_BYTES)
    if result.status != 200:
        return None  # asked again at the next cycle
    return parse_page(source, ext_id, result.content.decode("latin-1"))


def search(client: httpx2.Client, source: str, begin: datetime, end: datetime, **criteria: str
           ) -> tuple[list[Declaration], bool]:
    """Lines of the search form for these criteria (cust_name, re_name, rn_txt, ls_cnty_name), and whether the
    answer was cut at its 50 lines."""
    params = {"fuseaction": "main.searchresults", "newsearch": "yes", **criteria,
              "event_start_beg_dt": f"{begin.astimezone(TEXAS_TIME):%m/%d/%Y}",
              "event_start_end_dt": f"{end.astimezone(TEXAS_TIME):%m/%d/%Y}"}
    result = fetch(client, SEARCH, params=params, max_bytes=MAX_BYTES)
    if result.status >= 500 and RETRY_PAUSE:  # the regulator's site answers 522 when asked too fast
        time.sleep(RETRY_PAUSE)
        result = fetch(client, SEARCH, params=params, max_bytes=MAX_BYTES)
    if result.status != 200:
        raise TceqError(f"HTTP {result.status}")
    if PACE:
        time.sleep(PACE)
    out = []
    for m in _ROW.finditer(result.content.decode("latin-1")):
        cells = [clean_text(c, 200) for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(0), re.S)]
        if len(cells) < 9 or not re.fullmatch(r"RN\d+", cells[1]):
            continue
        ended = when(cells[4])
        out.append(Declaration(
            source, m.group(1), ref=cells[1], entity=cells[2], owner=re.sub(r"^CN\d+\s*", "", cells[8]),
            kind=cells[5].upper(), started_at=when(cells[3]), ended_at=ended,
            # An old line without a final report will never get one: it is not followed.
            final=cells[6].upper() == "FINAL" or (ended is not None and end - ended > REREAD_FOR),
            detailed=False, url=DETAIL.format(ext_id=m.group(1))))
    return out, len(out) >= SEARCH_CAP


def history(client: httpx2.Client, source: str, ref: str, begin: datetime, end: datetime, depth: int = 0
            ) -> list[Declaration]:
    """Every declaration of an installation over a period: year by year, a cut answer asked again in two halves."""
    out: list[Declaration] = []
    start = begin
    while start < end:
        stop = min(start + timedelta(days=366), end) if depth == 0 else end
        rows, cut = search(client, source, start, stop, rn_txt=ref)
        if cut and depth < 4 and stop - start > timedelta(days=4):
            middle = start + (stop - start) / 2
            rows = [*history(client, source, ref, start, middle, depth + 1),
                    *history(client, source, ref, middle, stop, depth + 1)]
        out.extend(rows)
        start = stop
    return list({d.ext_id: d for d in out}.values())


def feed(client: httpx2.Client, source: str, url: str, now: datetime) -> list[Declaration]:
    """The declarations of the last 7 days, as the feed gives them: number, installation, name, start. Texas files
    some ten declarations a day: an empty feed, or one whose lines no longer read, is a broken source, not a calm
    week."""
    entries = _feed_entries(client, url, now - FEED_WINDOW)
    out = []
    for title, _summary, link, published in entries:
        m = _TITLE.match(title)
        if m and link == DETAIL.format(ext_id=m.group(1)):
            out.append(Declaration(source, m.group(1), ref=m.group(2), entity=m.group(3).strip(),
                                   started_at=published, detailed=False, url=link))
    if not out:
        raise TceqError("flux vide : source muette" if not entries else "flux illisible : format changé")
    return out


# --- which installation is which site ------------------------------------------------------------------------------
def in_scope(site: Site, ident: identity.Identity | None) -> bool | None:
    """True: a site of Texas, or a company without any place (all its installations count). False: known to be in
    another state. None: a place whose state nothing tells."""
    if site.country_iso2 != "US":
        return False
    if ident is not None and ident.state:
        return ident.state == "TX"
    if not site.place:
        return True
    if re.search(r"(?<![a-z])(texas|tx)(?![a-z])", fold(site.place)):
        return True
    return None


def _subject(site: Site, ident: identity.Identity | None) -> str:
    extra = f"|{','.join(ident.names)}|{ident.county}|{ident.state}" if ident else ""
    return identity.subject(site) + extra


def _text(d: Declaration) -> str:
    return f"{d.entity} {d.county} texas"


def identify(client: httpx2.Client, source: str, site: Site, ident: identity.Identity | None, now: datetime
             ) -> list[Declaration]:
    """Lines of the registry that name the site. Searched by owner and by installation name, for the site's own
    first word and for the first word of each legal name of its operator, in its county when it is known. An answer
    cut at 50 lines that names nothing is asked again with a word of the site's place."""
    words = list(dict.fromkeys([site.lead, *(a[0] for a in site.aliases)]))[:3]
    criteria = {"ls_cnty_name": ident.county} if ident and ident.county and site.places else {}
    begin = now - timedelta(days=366 * 10)
    found: dict[str, Declaration] = {}

    def ask(**fields: str) -> bool:
        rows, cut = search(client, source, begin, now, usefullwildcard="yes", **criteria, **fields)
        for d in rows:
            d.county = criteria.get("ls_cnty_name", "")
            if match_sites(_text(d), [site], owner=d.owner):
                found[d.ext_id] = d
        return cut

    cut = False
    for word in words:
        cut = ask(cust_name=word.upper()) or cut
        cut = ask(re_name=word.upper()) or cut
    if cut and not found:
        for place in sorted(site.places - set(site.company), key=len, reverse=True)[:2]:
            ask(cust_name=site.lead.upper(), re_name=place.upper())
    return list(found.values())


def catch_up(db: Database, client: httpx2.Client, source: str, sites: list[Site],
             idents: dict[tuple[str, str], identity.Identity], now: datetime, budget: Budget) -> list[str]:
    """Coverage of each site (outside, found, not found), then the past of the installations just bound. A failed
    search is tried again at the next cycle; the failures are returned (kind of error only) for the diagnostic."""
    failures: list[str] = []
    with db.connect() as conn:
        covered = records.coverage(conn, source)
    asked = 0
    for site in sites:
        key = (site.material_id, site.node_key)
        ident = idents.get(key)
        scope = in_scope(site, ident)
        row = covered.get(key)
        subject = _subject(site, ident)
        if row is not None and row["subject"] == subject and (
                row["status"] != "none" or now - datetime.fromisoformat(row["checked_at"]) < RECHECK_NONE):
            continue
        if scope is False:
            with db.transaction() as conn:
                records.set_coverage(conn, source, site, subject, "outside", now)
            continue
        if scope is None and identity.askable(site) and ident is None:
            continue  # its state is being asked: wait for the answer before searching
        if asked >= budget.sites:
            continue
        asked += 1
        try:
            rows = identify(client, source, site, ident, now)
        except (httpx2.HTTPError, ValueError, SourceError) as exc:
            failures.append(f"recherche d'un site : {type(exc).__name__} {str(exc)[:80]}")
            continue
        with db.transaction() as conn:
            for d in rows:
                records.save(conn, d, now)
                records.bind(conn, source, d.ref, site, "search", d.entity, d.owner, now)
            has = conn.execute("SELECT 1 FROM registry_installations WHERE source = ? AND material_id = ? "
                               "AND node_key = ? AND subject = ?", (source, *key, site.subject)).fetchone()
            records.set_coverage(conn, source, site, subject, "bound" if has else "none", now)
    with db.connect() as conn:
        todo = [r[0] for r in conn.execute(
            "SELECT DISTINCT ref FROM registry_installations WHERE source = ? AND history_from IS NULL "
            "ORDER BY bound_at", (source,))][:budget.backfills]
    begin = now - timedelta(days=365 * HISTORY_YEARS)
    for ref in todo:
        try:
            rows = history(client, source, ref, begin, now)
        except (httpx2.HTTPError, ValueError, SourceError) as exc:
            failures.append(f"passé de {ref} : {type(exc).__name__} {str(exc)[:80]}")
            continue
        with db.transaction() as conn:
            for d in rows:
                records.save(conn, d, now)
            conn.execute("UPDATE registry_installations SET history_from = ? WHERE source = ? AND ref = ?",
                         (iso(begin), source, ref))
    return failures


# --- the cycle -----------------------------------------------------------------------------------------------------
def item(source: str, d: Declaration, matched: list[Site], now: datetime, reasons: list[str], prof: records.Profile,
         cited: list[str], final_note: str = "") -> Item:
    what = TYPES.get(d.kind, d.kind.lower() or "déclaration")
    title = f"{d.entity} ({d.owner}, Texas) : {what}, incident {d.ext_id}"
    hours = d.hours(now)
    local = "%d/%m/%Y %H:%M"
    period = ""
    if d.started_at is not None:
        ahead = d.started_at > now
        period = f"{'Début annoncé pour le' if ahead else 'Début le'} {d.started_at.astimezone(TEXAS_TIME):{local}}"
        if ahead and d.ended_at is not None:
            period += f", fin prévue le {d.ended_at.astimezone(TEXAS_TIME):{local}}"
        elif d.ongoing(now):
            period += ", toujours en cours"
        elif d.ended_at is not None:
            period += f", fin le {d.ended_at.astimezone(TEXAS_TIME):{local}}"
        period += f" (heure du Texas ; {records._h(hours or 0.0)})."
    parts = [
        f"{final_note[:1].upper()}{final_note[1:]}." if final_note else "",
        f"Signalé parce que : {' ; '.join(reasons)}." if reasons else "",
        period,
        f"Unité : {d.unit[:120]}." if d.unit else "",
        f"Cause déclarée : {d.cause[:330]}" if d.cause else "",
        f"La déclaration cite : {', '.join(cited[:3])}." if cited else "",
        prof.sentence(),
        f"Régulateur prévenu le {d.notified_at.astimezone(TEXAS_TIME):{local}}." if d.notified_at else "",
        "Rapport final." if d.final else "Premier rapport, la durée peut encore changer.",
    ]
    out = _record(source, LABEL, (("Rapport final : " if final_note else "") + title)[:300],
                  " ".join(p for p in parts if p)[:850], d.url, d.notified_at or d.started_at, "en", matched)
    if final_note:
        out.key = d.url + "#final"  # a second article of the same declaration: it prolongs the same event
    return out


def collect(db: Database, client: httpx2.Client, source: str, url: str, now: datetime, sites: list[Site],
            material_names: dict[str, list[str]] | None = None, budget: Budget | None = None) -> list[Item]:
    """One cycle: who the sites are, the feed, the pages worth reading, and what departs from each site's habit."""
    budget = budget or Budget()
    us = [s for s in sites if s.country_iso2 == "US"]
    if not us:
        return []
    idents = identity.ensure(db, client, us, now, budget.identities)
    us = identity.apply(us, idents)
    failures = catch_up(db, client, source, us, idents, now, budget)
    if failures:
        log.warning("registry catch-up: source=%s failures=%s first=%s", source, len(failures), failures[0][:60])
    eligible = [s for s in us if in_scope(s, idents.get((s.material_id, s.node_key))) is not False]
    entries = feed(client, source, url, now)
    with db.connect() as conn:
        binding = records.bound(conn, source, us)
        known = {r["ext_id"]: r for r in conn.execute(
            "SELECT * FROM official_records WHERE source = ? AND first_seen_at >= ?",
            (source, iso(now - REREAD_FOR - FEED_WINDOW)))}
        seen_refs = {r[0] for r in conn.execute(
            "SELECT DISTINCT ref FROM official_records WHERE source = ? AND detailed = 1", (source,))}
    fresh: list[Declaration] = []
    asked = 0
    for e in entries:
        row = known.get(e.ext_id)
        followed = e.ref in binding
        # A line found by a search while the site was being identified is still to be read and judged.
        due = row is not None and followed and (
            not row["detailed"] or records.worth_following(row, now, REREAD_EVERY, REREAD_FOR))
        if row is not None and not due:
            continue
        if row is None and not followed and e.ref in seen_refs:
            fresh.append(e)  # an installation already read and not a site of the chains: the feed's line is enough
            continue
        if asked >= budget.pages:
            continue
        asked += 1
        d = page(client, source, e.ext_id)
        if d is not None:
            fresh.append(d)
    if asked >= PAGES_PROOF and not any(d.detailed for d in fresh):
        raise TceqError("fiches illisibles : format changé")
    in_feed = {e.ext_id for e in entries}
    for ext_id, row in known.items():  # declarations that left the feed before their final report
        if (ext_id not in in_feed and row["ref"] in binding and asked < budget.pages
                and records.worth_following(row, now, REREAD_EVERY, REREAD_FOR)):
            asked += 1
            d = page(client, source, ext_id)
            if d is not None:
                fresh.append(d)
    items: list[Item] = []
    with db.transaction() as conn:
        # Everything is kept first: a declaration is judged with the others of its week in hand.
        before = {d.ext_id: records.get(conn, source, d.ext_id) for d in fresh}
        for d in fresh:
            matched = binding.get(d.ref, [])
            if not matched and d.detailed and d.ref:
                matched = match_sites(_text(d), eligible, owner=d.owner)
                for site in matched:
                    records.bind(conn, source, d.ref, site, "name", d.entity, d.owner, now)
                    records.set_coverage(conn, source, site, _subject(
                        site, idents.get((site.material_id, site.node_key))), "bound", now)
                if matched:
                    binding[d.ref] = matched
            records.save(conn, d, now)
        for d in fresh:
            matched = binding.get(d.ref, [])
            previous = before[d.ext_id]
            if not matched or not d.detailed:
                continue
            prof = records.profile(conn, d, now)
            cited = sorted({n for s in matched for n in records.names_material(
                d, (material_names or {}).get(s.material_id, []))})
            if previous is None or previous["emitted"] == "":
                verdict = records.judge(conn, d, now, prof)
                if verdict.notable:
                    items.append(item(source, d, matched, now, verdict.reasons, prof, cited))
                    records.mark_emitted(conn, source, d.ext_id, "first")
            else:
                note = records.follow_up(previous, d, now)
                if note:
                    items.append(item(source, d, matched, now, [], prof, cited, final_note=note))
                    records.mark_emitted(conn, source, d.ext_id, "final")
    return items
