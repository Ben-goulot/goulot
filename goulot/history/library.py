"""Library of past cases: preparation by the AI in two calls, checks by the code, review by the user
(docs/SPEC.md, section 6).

Call 1 (`case_research_v2`): free web research, cited, with the leads of public databases in `<leads>`. The code
replaces each citation with `[srcN]` and keeps the URLs; a lead cited as `[leadN]` joins the sources with the
address built by the code. Call 2 (`case_structure_v1`, `historical_case_v1`): structured cases without tools,
never seeing a URL. The code then checks every case (known sources, at least one of reliability ≥ 0.7, dates,
commodities of the material or of its direct inputs, share between 0 and 100) and stores it as a draft. Only a
case verified by the user, sources at hand, counts in a median or in an analysis dossier.

Only public data reach the prompts: the chain type (name, CAS, direct inputs, producing countries and sites),
never Ma chaîne nor any coverage, quote or purchase.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx2

from goulot.chain import store as chain_store
from goulot.chain.research import WEB_SEARCH_TOOL, build_report
from goulot.collect.base import fold
from goulot.db.connection import Database, utcnow
from goulot.history import leads as leads_mod
from goulot.llm.backend import MessageBackend
from goulot.llm.budget import Budget, BudgetExceeded, CircuitOpen
from goulot.llm.calls import CallSpec, LLMError, call
from goulot.llm.pricing import Usage, cost_usd, web_search_price
from goulot.llm.prompts import escape, load_prompt, tag
from goulot.llm.validate import SchemaError, parse_json_output, schema, validate_schema
from goulot.pipeline.corroborate import domain_reliability

log = logging.getLogger("goulot.history")

RESEARCH_PROMPT = "case_research_v2"  # v1 found 7 events for acrylonitrile: too narrow
STRUCTURE_PROMPT = "case_structure_v1"
SCHEMA = "historical_case_v1"
MODEL = "claude-sonnet-5-5"
MAX_SEARCHES = 10
RESEARCH_MAX_TOKENS = 24_000
STRUCTURE_MAX_TOKENS = 16_000
DEFAULT_SINCE = date(2013, 1, 1)  # the customs unit prices go back to 2013
MIN_RELIABILITY = 0.7
MAX_CASES = 20
# Token estimates for the quote, from the reconstruction runs of phase 2 scaled to 10 searches.
RESEARCH_TOKENS = {"expected": (220_000, 8_000), "max": (380_000, RESEARCH_MAX_TOKENS)}
STRUCTURE_TOKENS = {"expected": (15_000, 7_000), "max": (30_000, STRUCTURE_MAX_TOKENS)}
LEAD_MARKER = re.compile(r"\[(lead\d+)\]")
DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
CASE_ID_RE = re.compile(r"[^a-z0-9_]+")
ENUM_FIELDS = ("event_type", "node_type", "direction", "confidence")


class CaseError(LLMError):
    """Message in French, shown on the « Bibliothèque de cas » screen."""


@dataclass(frozen=True)
class CaseQuote:
    estimate_usd: float
    max_usd: float
    searches_max: int


def cases_quote() -> CaseQuote:
    search = MAX_SEARCHES * web_search_price()

    def tokens(pair: tuple[int, int]) -> float:
        return cost_usd(MODEL, Usage(input_tokens=pair[0], output_tokens=pair[1]))

    return CaseQuote(
        estimate_usd=round(tokens(RESEARCH_TOKENS["expected"]) + search + tokens(STRUCTURE_TOKENS["expected"]), 2),
        max_usd=round(tokens(RESEARCH_TOKENS["max"]) + search + tokens(STRUCTURE_TOKENS["max"]), 2),
        searches_max=MAX_SEARCHES,
    )


# --- what the prompts receive (public chain type only) --------------------------------------------------------
@dataclass
class CaseMaterial:
    name: str
    cas: str
    inputs: list[str]
    countries: list[str]
    sites: list[str]
    since: date

    def commodities(self) -> set[str]:
        return {fold(n) for n in [self.name, *self.inputs]}


def case_material(conn: sqlite3.Connection, material_id: str, since: date | None = None) -> CaseMaterial:
    material = chain_store.get_material(conn, material_id)
    if material is None:
        raise ValueError("unknown material")
    nodes = conn.execute("SELECT * FROM chain_nodes WHERE material_id = ? AND valid_to IS NULL ORDER BY tier, "
                         "COALESCE(global_share_pct, -1) DESC", (material_id,)).fetchall()
    from goulot.prices.collect import direct_inputs

    inputs = [n["product"] or n["name"] for n in direct_inputs(conn, material_id)]
    producing = [n for n in nodes if n["node_type"] in ("plant", "mine", "recycler", "country")]
    countries = list(dict.fromkeys(n["country_iso2"] for n in producing if len(n["country_iso2"]) == 2))
    sites = [n["name"] for n in producing if n["node_type"] in ("plant", "mine", "recycler")]
    first = conn.execute("SELECT MIN(p.day) FROM prices p JOIN instrument_links l ON l.instrument_id = p.instrument_id "
                         "WHERE l.material_id = ? AND l.role != 'factor'", (material_id,)).fetchone()[0]
    start = since or (max(date.fromisoformat(first), DEFAULT_SINCE) if first else DEFAULT_SINCE)
    return CaseMaterial(material["name"], material["cas"], list(dict.fromkeys(inputs))[:8], countries[:10],
                        sites[:12], start)


def material_block(m: CaseMaterial) -> str:
    lines = [f"name: {escape(m.name)}", f"cas: {escape(m.cas)}",
             "direct_inputs: " + (escape(", ".join(m.inputs)) or "unknown"),
             "producing_countries: " + (escape(", ".join(m.countries)) or "unknown"),
             "main_sites: " + (escape(", ".join(m.sites)) or "unknown")]
    return tag("material", "\n".join(lines)) + "\n\n" + tag("since", m.since.isoformat())


def research_params(m: CaseMaterial, leads: list[leads_mod.Lead]) -> dict[str, Any]:
    user = material_block(m)
    if leads:
        user += "\n\n" + tag("leads", "\n".join(escape(lead.line()) for lead in leads))
    return {
        "model": MODEL,
        "max_tokens": RESEARCH_MAX_TOKENS,
        "system": load_prompt(RESEARCH_PROMPT),
        "tools": [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": MAX_SEARCHES,
                   "allowed_callers": ["direct"]}],
        "messages": [{"role": "user", "content": user}],
    }


def _sources_block(sources: list[dict[str, Any]]) -> str:
    lines = [f"{escape(s['ref'])} | {escape(s.get('title') or 'unknown')} | {escape(s.get('publisher') or 'unknown')}"
             f" | {escape(s.get('page_age') or 'unknown')}" for s in sources]
    return tag("sources", "\n".join(lines) or "(aucune)")


def structure_params(m: CaseMaterial, report: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    material = tag("material", "\n".join([f"name: {escape(m.name)}", f"cas: {escape(m.cas)}",
                                          "direct_inputs: " + (escape(", ".join(m.inputs)) or "unknown")]))
    user = "\n\n".join([material, tag("research_report", escape(report)), _sources_block(sources)])
    return {
        "model": MODEL,
        "max_tokens": STRUCTURE_MAX_TOKENS,
        "system": [{"type": "text", "text": load_prompt(STRUCTURE_PROMPT),
                    "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": schema(SCHEMA)}},
        "messages": [{"role": "user", "content": user}],
    }


def with_leads(report: str, sources: list[dict[str, Any]], leads: list[leads_mod.Lead]) -> list[dict[str, Any]]:
    """The web sources, then the leads the report cites, with the address built by the code."""
    cited = set(LEAD_MARKER.findall(report))
    return [*sources, *({"ref": lead.ref, "url": lead.url, "title": lead.title, "publisher": "gdacs.org",
                         "page_age": lead.day.isoformat()} for lead in leads if lead.ref in cited)]


# --- checks by the code ---------------------------------------------------------------------------------------
@dataclass
class CheckedCase:
    data: dict[str, Any]
    anomalies: list[str] = field(default_factory=list)


def reliability(url: str) -> float:
    parts = urlsplit(url)
    return domain_reliability(parts.hostname or "", parts.path)


def _date(text: str) -> date | None:
    if not DATE_RE.match(text):
        return None
    try:
        return date.fromisoformat(text if len(text) == 10 else text + "-01")
    except ValueError:
        return None


def check_cases(result: dict[str, Any], m: CaseMaterial, sources: list[dict[str, Any]],
                today: date) -> list[CheckedCase]:
    """Cases kept, each with its anomalies; a case without a valid date or commodity is dropped."""
    by_ref = {s["ref"]: s for s in sources}
    known = m.commodities()
    out: list[CheckedCase] = []
    seen_ids: set[str] = set()
    for raw in result["cases"][:MAX_CASES]:
        case = {**raw, **{k: str(raw[k]).lower() for k in ENUM_FIELDS}}
        anomalies: list[str] = []
        first = _date(str(case["first_report_date"]))
        if first is None or first > today or first < date(m.since.year - 1, 1, 1):
            log.info("case dropped: invalid first report date")
            continue
        if case["end_date"] != "unknown" and _date(str(case["end_date"])) is None:
            case["end_date"] = "unknown"
        affected = [fold(c) for c in case["affected_commodities"] if fold(c) in known]
        if not affected:
            log.info("case dropped: commodity outside the material and its inputs")
            continue
        case["affected_commodities"] = affected
        share = case["capacity_share_pct"]
        if share is not None and not 0 <= share <= 100:
            case["capacity_share_pct"] = None
            anomalies.append("Part de capacité hors de 0 à 100 % : retirée.")
        refs = [r for r in case["source_ids"] if r in by_ref]
        if len(refs) != len(case["source_ids"]):
            anomalies.append("Source inconnue citée : retirée.")
        case["source_ids"] = list(dict.fromkeys(refs))
        if not refs:
            log.info("case dropped: no known source")
            continue
        if max(reliability(by_ref[r]["url"]) for r in refs) < MIN_RELIABILITY:
            anomalies.append("Aucune source de fiabilité suffisante (0,7) : cas non vérifiable.")
        base = CASE_ID_RE.sub("_", str(case["case_id"]).lower()).strip("_")[:60] or "cas"
        case_id, n = base, 2
        while case_id in seen_ids:
            case_id, n = f"{base}_{n}", n + 1
        seen_ids.add(case_id)
        case["case_id"] = case_id
        out.append(CheckedCase(case, anomalies))
    return out


def store_cases(conn: sqlite3.Connection, material_id: str, job_id: str, cases: list[CheckedCase],
                sources: list[dict[str, Any]], report: str, injection: bool) -> int:
    """New drafts; a case already in the library (same id) is left as it is, verified or not."""
    by_ref = {s["ref"]: s for s in sources}
    now = utcnow()
    conn.execute("INSERT INTO case_reports (material_id, job_id, report, injection_suspected, created_at) "
                 "VALUES (?, ?, ?, ?, ?)", (material_id, job_id, report, int(injection), now))
    added = 0
    for c in cases:
        d = c.data
        cur = conn.execute(
            "INSERT OR IGNORE INTO historical_events (material_id, case_id, title, event_type, first_report_date, "
            "end_date, place, country_iso2, node_type, affected, capacity_share_pct, capacity_share_basis, direction, "
            "duration_days, summary, confidence, status, anomalies, job_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
            (material_id, d["case_id"], d["title_fr"][:200], d["event_type"], d["first_report_date"], d["end_date"],
             d["place"][:120], str(d["country_iso2"]).upper()[:7], d["node_type"],
             json.dumps(d["affected_commodities"], ensure_ascii=False), d["capacity_share_pct"],
             d["capacity_share_basis"][:200], d["direction"], d["duration_days"], d["summary_fr"][:1500],
             d["confidence"], json.dumps(c.anomalies, ensure_ascii=False), job_id, now))
        if cur.rowcount == 0:
            continue
        added += 1
        for ref in d["source_ids"]:
            s = by_ref[ref]
            conn.execute("INSERT OR IGNORE INTO case_sources (case_row, ref, url, title, publisher, page_age, "
                         "reliability) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (cur.lastrowid, ref, s["url"], s.get("title", "")[:300], s.get("publisher", "")[:120],
                          s.get("page_age", "")[:40], reliability(s["url"])))
    return added


# --- the preparation job --------------------------------------------------------------------------------------
@dataclass
class CaseDeps:
    db: Database
    budget: Budget
    backend: MessageBackend
    http: httpx2.Client | None  # for the leads; None in replays
    recorded_leads: list[leads_mod.Lead] | None = None  # replays: the leads of the recording
    on_leads: Callable[[list[leads_mod.Lead]], None] | None = None  # recordings: keep them


def prepare(deps: CaseDeps, material_id: str, job_id: str, *, budget_name: str = "reconstruction",
            today: date | None = None) -> float:
    """Research, structuring, checks and drafts. Returns the cost; the job records the outcome."""
    from goulot.chain.service import _finish, _step

    today = today or datetime.now(UTC).date()
    quote = cases_quote()
    try:
        with deps.db.connect() as conn:
            m = case_material(conn, material_id)
        if deps.recorded_leads is not None:
            found_leads = deps.recorded_leads
        else:
            found_leads = leads_mod.gdacs(deps.http, set(m.countries), m.since, today) if deps.http else []
        if deps.on_leads is not None:
            deps.on_leads(found_leads)
        _step(deps.db, job_id, 2, 0.0)
        research = call(deps.backend, deps.budget, CallSpec(
            key=f"case_research_{m.cas}", purpose="case_research", budget=budget_name,
            max_cost_usd=quote.max_usd, material_id=material_id), research_params(m, found_leads))
        report, sources = build_report(research.content)
        sources = with_leads(report, sources, found_leads)
        if not sources:
            raise CaseError("La recherche n'a renvoyé aucune source citée : aucun cas préparé.")
        _step(deps.db, job_id, 3, research.cost_usd)
        structured = call(deps.backend, deps.budget, CallSpec(
            key=f"case_structure_{m.cas}", purpose="case_structure", budget=budget_name,
            max_cost_usd=quote.max_usd, material_id=material_id), structure_params(m, report, sources))
        try:
            result = validate_schema(SCHEMA, parse_json_output(structured.content))
        except SchemaError:
            raise CaseError("La sortie de l'IA ne respecte pas le schéma des cas : aucun cas préparé.") from None
        if result["material_cas"] != m.cas:
            raise CaseError("La sortie de l'IA porte sur une autre matière : aucun cas préparé.")
        checked = check_cases(result, m, sources, today)
        with deps.db.transaction() as conn:
            added = store_cases(conn, material_id, job_id, checked, sources, report,
                                bool(result["injection_suspected"]))
    except (LLMError, BudgetExceeded, CircuitOpen) as exc:
        return _finish(deps.db, job_id, material_id, ok=False, message=str(exc))
    except Exception:
        log.exception("case preparation failed")
        _finish(deps.db, job_id, material_id, ok=False, message="Erreur interne pendant la préparation des cas.")
        raise
    message = f"{added} cas préparés, à vérifier un par un."
    if result["injection_suspected"]:
        message += " Contenu piégé signalé : à vérifier."
    return _finish(deps.db, job_id, material_id, ok=True, message=message)


def start_job(db: Database, material_id: str) -> str:
    import uuid

    job_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute("INSERT INTO jobs (id, material_id, kind, step, state, started_at) VALUES (?, ?, 'cases', 1, "
                     "'running', ?)", (job_id, material_id, utcnow()))
    return job_id


# --- review and listing ---------------------------------------------------------------------------------------
class ReviewError(ValueError):
    """Message in French."""


def review(conn: sqlite3.Connection, material_id: str, case_row: int, action: str) -> None:
    row = conn.execute("SELECT status, anomalies FROM historical_events WHERE id = ? AND material_id = ?",
                       (case_row, material_id)).fetchone()
    if row is None:
        raise ReviewError("Cas introuvable.")
    if action == "verify" and any("non vérifiable" in a for a in json.loads(row["anomalies"])):
        raise ReviewError("Ce cas n'a aucune source assez fiable : il ne peut pas être vérifié.")
    status = {"verify": "verified", "reject": "rejected", "reopen": "draft"}[action]
    conn.execute("UPDATE historical_events SET status = ?, reviewed_at = ? WHERE id = ?",
                 (status, utcnow(), case_row))


def cases(conn: sqlite3.Connection, material_id: str, *, statuses: tuple[str, ...] = ("draft", "verified"),
          before: date | None = None) -> list[dict[str, Any]]:
    """Cases of the material with their sources; `before`: only those announced before that day (replay)."""
    out = []
    for r in conn.execute("SELECT * FROM historical_events WHERE material_id = ? ORDER BY first_report_date",
                          (material_id,)):
        if r["status"] not in statuses:
            continue
        day = _date(r["first_report_date"])
        if day is None or (before is not None and day >= before):
            continue
        c = dict(r)
        c["affected"] = json.loads(r["affected"])
        c["anomalies"] = json.loads(r["anomalies"])
        c["day"] = day
        c["sources"] = [dict(s) for s in conn.execute(
            "SELECT ref, url, title, publisher, page_age, reliability FROM case_sources WHERE case_row = ? "
            "ORDER BY ref", (r["id"],))]
        out.append(c)
    return out
