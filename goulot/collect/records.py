"""Official records followed over time (docs/SPEC.md, section 4, « Registres »).

A declaration to a regulator is not an article: it has a number, an installation with its own number, a start, an
end, a first and a final report. So it is kept (table `official_records`) and judged by code against the past of
its own installation, before anything is sent to the watch:

- an installation recognized once as a site of a chain is bound by its number (`registry_installations`);
- each site has a coverage status (`registry_coverage`): followed, not found, or outside what the registry covers.
  A site that is not followed is never shown as calm;
- a plant that flares for two hours as it does five times a year is routine: stored, counted, not sent. What is
  sent is what departs from its habit, and the excerpt says by how much.

The rules below were set on real history (replay of 2015-2026 for two acrylonitrile plants of Texas, 14 November
2026): median unplanned declaration 1.7 h; the three real outages lasted 280 h, 308 h and 463 h.
"""

from __future__ import annotations

import math
import re
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from goulot.collect.base import fold, iso
from goulot.collect.sitematch import Site

UNPLANNED = frozenset({"EMISSIONS EVENT", "EXCESS OPACITY"})
RESTART = "AIR STARTUP"
LONG_HOURS = 24.0  # an unplanned event that lasts a day is no flare of an hour
LONG_TIMES_USUAL = 3.0  # … unless the site's usual incident already lasts hours: then three times its median
PLANNED_HOURS = 72.0
TIMES_USUAL = 5.0
MIN_UNUSUAL_HOURS = 6.0
MIN_HISTORY = 4  # declarations needed before « usual » means anything
CLUSTER = 3
CLUSTER_WINDOW = timedelta(days=7)
HABIT_WINDOW = timedelta(days=550)
HABIT_TOLERANCE = 0.25
PROFILE_YEARS = 5
FOLLOW_UP_HOURS = 24.0
AFTER_NOTABLE = timedelta(days=60)
# What stops a plant, in a declared cause (English: the registries read so far are American).
SERIOUS = re.compile(
    r"\bfire\b(?! ?(?:water|box))|explosion|\bexplod|power (?:outage|failure|loss|interruption)"
    r"|power supply (?:from|was|is|interrupt)|loss of (?:electrical |electric )?power|electrical (?:outage|failure)"
    r"|(?:facility|plant|site)[- ]?wide|hurricane|tropical storm|winter storm|\bfreez|lightning|evacuat|injur"
    r"|fatal|force majeure")


@dataclass
class Declaration:
    source: str
    ext_id: str
    ref: str = ""
    entity: str = ""
    owner: str = ""
    county: str = ""
    kind: str = ""
    unit: str = ""
    cause: str = ""
    compounds: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    notified_at: datetime | None = None
    final: bool = False
    detailed: bool = True
    url: str = ""

    def hours(self, now: datetime) -> float | None:
        """Declared duration; an event without an end, or whose end is still ahead, has lasted until now."""
        if self.started_at is None:
            return None
        end = self.ended_at if self.ended_at is not None and (self.final or self.ended_at <= now) else now
        return max((end - self.started_at).total_seconds() / 3600, 0.0)

    def ongoing(self, now: datetime) -> bool:
        return not self.final and (self.ended_at is None or self.ended_at > now)


@dataclass(frozen=True)
class Profile:
    """The past of one installation, from its own unplanned declarations."""

    years: float = 0.0
    count: int = 0
    median_hours: float = 0.0
    longest_hours: float = 0.0
    longest_on: datetime | None = None

    def sentence(self) -> str:
        if self.count < MIN_HISTORY or self.years <= 0:
            return "Passé de ce site au registre : trop peu de déclarations pour dire ce qui est habituel."
        text = (f"Habitude de ce site au registre : {self.count} incidents déclarés en {self.years:.0f} ans, "
                f"durée médiane {_h(self.median_hours)}")
        if self.longest_on is not None and self.longest_hours >= LONG_HOURS:
            text += f" ; le plus long : {_h(self.longest_hours)} en {self.longest_on:%m/%Y}"
        return text + "."


@dataclass
class Verdict:
    reasons: list[str] = field(default_factory=list)  # French; empty: routine, nothing is sent

    @property
    def notable(self) -> bool:
        return bool(self.reasons)


def _h(hours: float) -> str:
    if hours >= 48:
        return f"{hours / 24:.0f} jours"
    return f"{hours:.0f} h" if hours >= 1 else f"{hours * 60:.0f} min"


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def from_row(r: sqlite3.Row) -> Declaration:
    return Declaration(r["source"], r["ext_id"], r["ref"], r["entity"], r["owner"], r["county"], r["kind"], r["unit"],
                       r["cause"], r["compounds"], _dt(r["started_at"]), _dt(r["ended_at"]), _dt(r["notified_at"]),
                       bool(r["final"]), bool(r["detailed"]), r["url"])


def get(conn: sqlite3.Connection, source: str, ext_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM official_records WHERE source = ? AND ext_id = ?", (source, ext_id)).fetchone()
    return row


def save(conn: sqlite3.Connection, d: Declaration, now: datetime) -> None:
    """Insert, or bring up to date. A line of a list never overwrites what the declaration's own page said."""
    values = (d.ref, d.entity, d.owner, d.county, d.kind.upper(), d.unit, d.cause, d.compounds, iso(d.started_at),
              iso(d.ended_at), iso(d.notified_at), int(d.final), int(d.detailed), d.url, iso(now))
    previous = get(conn, d.source, d.ext_id)
    if previous is None:
        conn.execute(
            "INSERT INTO official_records (source, ext_id, ref, entity, owner, county, kind, unit, cause, compounds, "
            "started_at, ended_at, notified_at, final, detailed, url, read_at, first_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (d.source, d.ext_id, *values, iso(now)))
    elif d.detailed or not previous["detailed"]:
        conn.execute(
            "UPDATE official_records SET ref = ?, entity = ?, owner = ?, county = ?, kind = ?, unit = ?, cause = ?, "
            "compounds = ?, started_at = ?, ended_at = ?, notified_at = ?, final = ?, detailed = ?, url = ?, "
            "read_at = ? WHERE id = ?", (*values, previous["id"]))


def mark_emitted(conn: sqlite3.Connection, source: str, ext_id: str, what: str) -> None:
    conn.execute("UPDATE official_records SET emitted = ? WHERE source = ? AND ext_id = ?", (what, source, ext_id))


# --- installations and coverage ---------------------------------------------------------------------------------
def bind(conn: sqlite3.Connection, source: str, ref: str, site: Site, how: str, entity: str, owner: str,
         now: datetime) -> bool:
    """True when this installation was not yet bound to that site."""
    cur = conn.execute(
        "INSERT INTO registry_installations (source, ref, material_id, node_key, subject, entity, owner, how, "
        "bound_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (source, ref, material_id, node_key) DO UPDATE SET "
        "subject = excluded.subject, entity = excluded.entity, owner = excluded.owner, how = excluded.how, "
        "bound_at = excluded.bound_at WHERE registry_installations.subject != excluded.subject",
        (source, ref, site.material_id, site.node_key, site.subject, entity, owner, how, iso(now)))
    return cur.rowcount == 1


def bound(conn: sqlite3.Connection, source: str, sites: list[Site]) -> dict[str, list[Site]]:
    """Installation number → sites of the current chains it is bound to. A binding to a node the chain no longer
    has, or has rebuilt as another site under the same key, is ignored, not deleted: the node may come back."""
    current = {(s.material_id, s.node_key): s for s in sites}
    out: dict[str, list[Site]] = {}
    for r in conn.execute("SELECT ref, material_id, node_key, subject FROM registry_installations WHERE source = ? "
                          "ORDER BY ref, material_id, node_key", (source,)):
        site = current.get((r["material_id"], r["node_key"]))
        if site is not None and site.subject == r["subject"]:
            out.setdefault(r["ref"], []).append(site)
    return out


def set_coverage(conn: sqlite3.Connection, source: str, site: Site, subject: str, status: str, now: datetime
                 ) -> None:
    conn.execute(
        "INSERT INTO registry_coverage (source, material_id, node_key, subject, status, checked_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (source, material_id, node_key) DO UPDATE SET "
        "subject = excluded.subject, status = excluded.status, checked_at = excluded.checked_at",
        (source, site.material_id, site.node_key, subject, status, iso(now)))


def coverage(conn: sqlite3.Connection, source: str) -> dict[tuple[str, str], sqlite3.Row]:
    return {(r["material_id"], r["node_key"]): r for r in conn.execute(
        "SELECT * FROM registry_coverage WHERE source = ?", (source,))}


# --- judging a declaration against the past of its installation ---------------------------------------------------
def _past(conn: sqlite3.Connection, d: Declaration, since: datetime) -> list[Declaration]:
    until = max(t for t in (d.started_at, d.notified_at, since) if t is not None) + CLUSTER_WINDOW
    return [from_row(r) for r in conn.execute(
        "SELECT * FROM official_records WHERE source = ? AND ref = ? AND ext_id != ? AND started_at >= ? "
        "AND started_at <= ? ORDER BY started_at", (d.source, d.ref, d.ext_id, iso(since), iso(until)))
            ] if d.ref else []


def profile(conn: sqlite3.Connection, d: Declaration, now: datetime) -> Profile:
    """Unplanned declarations of the same installation over the years its history was read for."""
    row = conn.execute("SELECT MIN(history_from) FROM registry_installations WHERE source = ? AND ref = ?",
                       (d.source, d.ref)).fetchone()
    start = max(_dt(row[0]) or now, now - timedelta(days=365 * PROFILE_YEARS)) if row and row[0] else None
    if start is None:
        return Profile()
    past = [p for p in _past(conn, d, start) if p.kind in UNPLANNED and p.started_at and p.started_at < now]
    hours = [(p.hours(now) or 0.0, p.started_at) for p in past]
    if not hours:
        return Profile(years=(now - start).days / 365)
    longest = max(hours, key=lambda t: t[0])
    return Profile((now - start).days / 365, len(hours), statistics.median(h for h, _ in hours), longest[0],
                   longest[1])


def judge(conn: sqlite3.Connection, d: Declaration, now: datetime, prof: Profile | None = None) -> Verdict:
    """Why a declaration departs from its installation's habit; no reason, no sending. A large complex that files
    twenty-five incidents a year is held to its own measure, not to a small plant's."""
    verdict = Verdict()
    kind = d.kind.upper()
    serious = SERIOUS.search(fold(f"{d.unit} {d.cause}")) is not None
    prof = prof if prof is not None else profile(conn, d, now)
    recent = _past(conn, d, now - HABIT_WINDOW)
    known = prof.count >= MIN_HISTORY
    if kind in UNPLANNED:
        hours = d.hours(now)
        if serious:
            verdict.reasons.append("la cause déclarée est de celles qui arrêtent une usine")
        if hours is not None and hours >= max(LONG_HOURS, LONG_TIMES_USUAL * prof.median_hours if known else 0.0):
            verdict.reasons.append(f"incident de {_h(hours)}" + (", toujours en cours" if d.ongoing(now) else ""))
        elif (hours is not None and known and hours >= MIN_UNUSUAL_HOURS and hours < LONG_HOURS
              and hours >= TIMES_USUAL * max(prof.median_hours, 0.1)):
            verdict.reasons.append(f"{hours / max(prof.median_hours, 0.1):.0f} fois la durée habituelle de ce site")
        if d.started_at is not None:
            close = [p for p in recent if p.kind in UNPLANNED and p.started_at
                     and abs(p.started_at - d.started_at) <= CLUSTER_WINDOW]
            weekly = prof.count / (prof.years * 52) if prof.years > 0 else 0.0
            if len(close) + 1 >= max(CLUSTER, math.ceil(2 + 4 * weekly)):
                verdict.reasons.append(f"{len(close) + 1} incidents déclarés par ce site en une semaine")
        return verdict
    # Planned activity: a start-up, a shutdown, maintenance. Its dates are declared, often ahead of time.
    hours = d.hours(max(now, d.ended_at or now))
    alike = [p for p in recent if p.kind == kind and hours is not None and (h := p.hours(now)) is not None
             and abs(h - hours) <= HABIT_TOLERANCE * hours]
    usual = len(alike) >= 2  # the same notice again and again: blanket monthly maintenance
    long_one = hours is not None and hours >= PLANNED_HOURS and not usual
    if kind == RESTART:
        sent = conn.execute(
            "SELECT 1 FROM official_records WHERE source = ? AND ref = ? AND ext_id != ? AND emitted != '' "
            "AND started_at >= ?", (d.source, d.ref, d.ext_id, iso(now - AFTER_NOTABLE))).fetchone()
        if serious or sent:
            verdict.reasons.append("redémarrage déclaré après un arrêt")
        elif long_one:
            verdict.reasons.append(f"démarrage de {_h(hours or 0.0)}")
    elif serious and not usual:
        verdict.reasons.append("arrêt ou travaux déclarés à la suite d'un sinistre")
    elif long_one:
        verdict.reasons.append(f"arrêt ou travaux programmés de {_h(hours or 0.0)}")
    return verdict


def names_material(d: Declaration, names: list[str]) -> list[str]:
    """Materials of the chains that the declaration itself cites (unit, cause, substances released)."""
    text = fold(f"{d.unit} {d.cause} {d.compounds}")
    return [n for n in names if len(n) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(fold(n)) + r"(?![a-z0-9])",
                                                           text)]


def worth_following(row: sqlite3.Row, now: datetime, every: timedelta, at_most: timedelta) -> bool:
    """A declaration without its final report is read again, at most once per `every`, for `at_most`."""
    if row["final"]:
        return False
    if not row["detailed"]:  # known from a list only: its page is read as soon as possible
        return now - datetime.fromisoformat(row["first_seen_at"]) <= at_most
    return (now - datetime.fromisoformat(row["first_seen_at"]) <= at_most
            and now - datetime.fromisoformat(row["read_at"]) >= every)


def follow_up(previous: sqlite3.Row, d: Declaration, now: datetime) -> str | None:
    """What changed in a declaration already sent that is worth saying again: its final report, when the duration
    it gives differs by a day or more from what was sent, or closes an event that was still running."""
    if previous["emitted"] != "first" or not d.final:
        return None
    before = from_row(previous)
    was, is_now = before.hours(_dt(previous["read_at"]) or now) or 0.0, d.hours(now) or 0.0
    if before.ongoing(_dt(previous["read_at"]) or now) or abs(is_now - was) >= FOLLOW_UP_HOURS:
        return f"rapport final : l'incident a duré {_h(is_now)}"
    return None


# --- what stopped a site before ------------------------------------------------------------------------------------
# Kinds of weather that stop plants, as their declared causes word them.
HAZARDS = {
    "freeze": re.compile(r"\bfreez|winter storm|winter weather|ice storm|arctic|extreme cold|cold weather"),
    "tropical": re.compile(r"hurricane|tropical storm|tropical depression|storm surge"),
}
HAZARD_NAMES = {"freeze": "un épisode de gel", "tropical": "un ouragan ou une tempête tropicale"}


def long_undetailed(conn: sqlite3.Connection, source: str, ref: str, now: datetime, before: datetime, limit: int
                    ) -> list[str]:
    """The longest past incidents of an installation whose cause was never read (known from a list only): their
    pages say what stopped the plant. Only those started before `before`: a recent one is read, and judged, by the
    cycle itself."""
    rows = [from_row(r) for r in conn.execute(
        "SELECT * FROM official_records WHERE source = ? AND ref = ? AND detailed = 0 AND started_at IS NOT NULL "
        "AND started_at < ?", (source, ref, iso(before)))]
    long_ones = sorted(((d.hours(now) or 0.0, d.ext_id) for d in rows if d.kind in UNPLANNED), reverse=True)
    return [ext_id for hours, ext_id in long_ones if hours >= LONG_HOURS][:limit]


def antecedent(conn: sqlite3.Connection, source: str, refs: list[str], hazard: str, now: datetime
               ) -> Declaration | None:
    """The longest incident of a day or more that these installations declared with a cause of that kind."""
    pattern = HAZARDS[hazard]
    best: tuple[float, Declaration] | None = None
    for ref in refs:
        for r in conn.execute("SELECT * FROM official_records WHERE source = ? AND ref = ? AND detailed = 1 "
                              "AND started_at IS NOT NULL AND started_at <= ?", (source, ref, iso(now))):
            d = from_row(r)
            hours = d.hours(now) or 0.0
            if (d.kind in UNPLANNED and hours >= LONG_HOURS and pattern.search(fold(d.cause))
                    and (best is None or hours > best[0])):
                best = (hours, d)
    return best[1] if best else None


def antecedent_sentence(d: Declaration | None, hazard: str, now: datetime) -> str:
    if d is None or d.started_at is None:
        return "aucun arrêt de ce genre déclaré au registre"
    return (f"arrêté ou perturbé {_h(d.hours(now) or 0.0)} par {HAZARD_NAMES[hazard]} en "
            f"{d.started_at:%m/%Y} (déclaration au registre)")


# --- what the user is shown of each site ---------------------------------------------------------------------------
REGISTRIES = {"tceq_emissions": "registre du Texas (TCEQ)"}


def current_subjects(conn: sqlite3.Connection, material_id: str) -> dict[str, str]:
    """node_key → name and place of the chain's current nodes, as `Site.subject` writes them."""
    return {r["node_key"]: f"{r['name']}|{'' if r['place'].strip().lower() == 'unknown' else r['place'].strip()}"
            for r in conn.execute("SELECT node_key, name, place FROM chain_nodes WHERE material_id = ? "
                                  "AND valid_to IS NULL", (material_id,))}


def site_summaries(conn: sqlite3.Connection, material_id: str, now: datetime,
                   subjects: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    """node_key → {status, text}: what an official registry knows of each site of a chain, in one French sentence.
    A site that is not followed is said so: its silence is not calm. `subjects`: node_key → name and place of the
    current nodes (read from the chain when not given)."""
    out: dict[str, dict[str, str]] = {}
    # A node renamed or rebuilt since the last cycle: what was worked out for the former site is not shown for it.
    current = subjects if subjects is not None else current_subjects(conn, material_id)
    for row in conn.execute("SELECT * FROM registry_coverage WHERE material_id = ?", (material_id,)):
        subject = current.get(row["node_key"])
        if subject is None or not row["subject"].startswith(subject):
            continue
        name = REGISTRIES.get(row["source"], row["source"])
        if row["status"] == "outside":
            text = f"Hors de ce que couvre le {name} : aucune déclaration officielle n'est lue pour ce site."
        elif row["status"] == "none":
            text = (f"Non trouvé au {name}. Ce site n'est pas suivi par ses déclarations : son silence ne dit rien.")
        else:
            refs = [r["ref"] for r in conn.execute(
                "SELECT ref FROM registry_installations WHERE source = ? AND material_id = ? AND node_key = ? "
                "AND subject = ? ORDER BY ref", (row["source"], material_id, row["node_key"], subject))]
            marks = ",".join("?" * len(refs))
            since = now - timedelta(days=365 * PROFILE_YEARS)
            past = [from_row(r) for r in conn.execute(
                f"SELECT * FROM official_records WHERE source = ? AND ref IN ({marks}) AND started_at >= ? "  # noqa: S608  # nosec B608 - placeholders only
                "AND started_at <= ? ORDER BY started_at", (row["source"], *refs, iso(since), iso(now)))]
            unplanned = [(p.hours(now) or 0.0, p.started_at) for p in past if p.kind in UNPLANNED and p.started_at]
            text = f"Suivi au {name} par {len(refs)} installation{'s' if len(refs) > 1 else ''}"
            if not unplanned:
                text += f" : aucun incident déclaré en {PROFILE_YEARS} ans."
            else:
                longest = max(unplanned, key=lambda t: t[0])
                last = max(t[1] for t in unplanned)
                text += (f" : {len(unplanned)} incidents déclarés en {PROFILE_YEARS} ans, durée médiane "
                         f"{_h(statistics.median(h for h, _ in unplanned))} ; le plus long, {_h(longest[0])}, en "
                         f"{longest[1]:%m/%Y} ; le dernier le {last:%d/%m/%Y}.")
        out[row["node_key"]] = {"status": row["status"], "text": text}
    return out
