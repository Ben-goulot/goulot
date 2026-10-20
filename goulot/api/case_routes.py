"""Routes of the « Bibliothèque de cas » and « Cas comparables » screens (docs/SPEC.md, sections 6 and 9).

Public data only (chain type, public sources, public prices): no vault token is needed and nothing from Ma chaîne
is read. Preparing cases spends on the reconstruction cap, after a quote confirmed by the user.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from goulot.api.context import AppContext, MissingAPIKey
from goulot.chain import store
from goulot.chain.quote import confirm_quote, save_quote
from goulot.history import library, reactions, similarity
from goulot.pipeline import analysis

log = logging.getLogger("goulot.api.cases")

UUID_RE = re.compile(r"^[0-9a-f-]{36}$")
EVENT_RE = re.compile(r"^EVT-\d{4}-\d{2}-\d{2}-\d{4}$")
QUOTE_KIND = "cases"


class PrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: str = Field(min_length=36, max_length=36)


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern=r"^(verify|reject|reopen)$")


def _reaction(r: reactions.Reaction) -> dict[str, Any]:
    return {"instrument_id": r.instrument_id, "name": r.name, "frequency": r.frequency,
            "base_day": r.base_day.isoformat(), "raw": {str(h): v for h, v in r.raw.items()},
            "own": {str(h): v for h, v in r.own.items()}, "beta": r.beta, "factor": r.factor, "notes": r.notes}


def _case(c: dict[str, Any], measured: list[reactions.Reaction], overlap: bool) -> dict[str, Any]:
    keep = ("id", "case_id", "title", "event_type", "first_report_date", "end_date", "place", "country_iso2",
            "node_type", "affected", "capacity_share_pct", "capacity_share_basis", "direction", "duration_days",
            "summary", "confidence", "status", "anomalies", "sources")
    return {**{k: c[k] for k in keep}, "overlap": overlap, "reactions": [_reaction(r) for r in measured]}


HORIZON_20 = {"daily": 20, "monthly": 1, "customs": 1, "tenday": 2}  # about 20 sessions


def _instrument(found: list[similarity.Analogue], hypothesis: dict[str, Any] | None) -> str | None:
    """The series shown on the card: the AI's hypothesis one, else the daily series most cases have."""
    ids = [r.instrument_id for a in found for r in a.reactions]
    if hypothesis and hypothesis["instrument_id"] in ids:
        return str(hypothesis["instrument_id"])
    daily = [r.instrument_id for a in found for r in a.reactions if r.frequency == "daily"]
    pool = daily or ids
    return max(set(pool), key=pool.count) if pool else None


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter(prefix="/api")

    def now() -> datetime:
        return datetime.fromtimestamp(ctx.clock.time(), UTC)

    def material_or_404(conn: Any, material_id: str) -> dict[str, Any]:
        if not UUID_RE.match(material_id):
            raise HTTPException(404, "Introuvable.")
        m = store.get_material(conn, material_id)
        if m is None:
            raise HTTPException(404, "Matière introuvable.")
        if m["status"] != "validated":
            raise HTTPException(409, "Validez d'abord la chaîne de cette matière.")
        return m

    @router.get("/materials/{material_id}/cases")
    def case_list(material_id: str) -> dict[str, Any]:
        with ctx.db.connect() as conn:
            m = material_or_404(conn, material_id)
            found = library.cases(conn, material_id, statuses=("draft", "verified", "rejected"))
            measured = reactions.for_cases(conn, material_id, found)
            overlap = reactions.overlapping({c["id"]: c["day"] for c in found if c["status"] != "rejected"})
            summaries = []
            ids = {r.instrument_id: r for rs in measured.values() for r in rs}
            for iid, r in ids.items():
                h = 20 if r.frequency == "daily" else reactions.horizons(r.frequency)[-1]
                s = reactions.summarize(found, measured, iid, h)
                summaries.append({"instrument_id": iid, "name": r.name, "horizon": h, "n": s.n,
                                  "median": s.median, "low": s.low, "high": s.high, "years": s.years,
                                  "excluded_overlap": s.excluded_overlap})
            job = conn.execute("SELECT id, state, step, message, cost_usd FROM jobs WHERE material_id = ? AND "
                               "kind = 'cases' ORDER BY started_at DESC LIMIT 1", (material_id,)).fetchone()
        return {"material": {"id": m["id"], "name": m["name"], "cas": m["cas"]},
                "cases": [_case(c, measured.get(c["id"], []), c["id"] in overlap) for c in found],
                "summaries": summaries, "job": dict(job) if job else None}

    @router.post("/materials/{material_id}/cases/quote")
    def case_quote(material_id: str) -> dict[str, Any]:
        with ctx.db.connect() as conn:
            m = material_or_404(conn, material_id)
        quote = library.cases_quote()
        with ctx.db.transaction() as conn:
            quote_id = save_quote(conn, m["id"], quote, now(), kind=QUOTE_KIND)
        return {"quote_id": quote_id, "estimate_usd": quote.estimate_usd, "max_usd": quote.max_usd,
                "searches_max": quote.searches_max, "remaining_usd": ctx.budget.remaining("reconstruction"),
                "cap_usd": ctx.budget.cap("reconstruction")}

    @router.post("/materials/{material_id}/cases/prepare")
    def case_prepare(material_id: str, body: PrepareBody) -> dict[str, str]:
        with ctx.db.connect() as conn:
            m = material_or_404(conn, material_id)
            running = conn.execute("SELECT 1 FROM jobs WHERE material_id = ? AND kind = 'cases' AND state = "
                                   "'running'", (material_id,)).fetchone()
        if running:
            raise HTTPException(409, "Une préparation de cas est déjà en cours pour cette matière.")
        with ctx.db.transaction() as conn:
            max_usd = confirm_quote(conn, body.quote_id, m["id"], now(), kind=QUOTE_KIND)
        if max_usd is None:
            raise HTTPException(409, "Devis expiré ou déjà utilisé : demandez un nouveau devis.")
        if ctx.budget.remaining("reconstruction") < max_usd:
            raise HTTPException(402, "Plafond mensuel des reconstructions insuffisant pour ce devis.")
        try:
            backend = ctx.llm_backend()
        except MissingAPIKey as exc:
            raise HTTPException(409, str(exc)) from None
        job_id = library.start_job(ctx.db, m["id"])

        def run() -> None:
            http = ctx.http_client()
            try:
                library.prepare(library.CaseDeps(ctx.db, ctx.budget, backend, http), m["id"], job_id)
            except Exception:
                log.exception("case preparation crashed")
            finally:
                http.close()

        ctx.jobs.submit(run)
        return {"job_id": job_id}

    @router.post("/materials/{material_id}/cases/{case_row}/review")
    def case_review(material_id: str, case_row: int, body: ReviewBody) -> dict[str, str]:
        with ctx.db.transaction() as conn:
            material_or_404(conn, material_id)
            try:
                library.review(conn, material_id, case_row, body.action)
            except library.ReviewError as exc:
                raise HTTPException(409, str(exc)) from None
        return {"status": "ok"}

    @router.get("/events/{event_id}/analogues")
    def event_analogues(event_id: str) -> dict[str, Any]:
        if not EVENT_RE.match(event_id):
            raise HTTPException(404, "Introuvable.")
        with ctx.db.connect() as conn:
            event = conn.execute("SELECT * FROM public_events WHERE id = ?", (event_id,)).fetchone()
            if event is None:
                raise HTTPException(404, "Événement introuvable.")
            target = similarity.target_for_event(conn, event)
            kept, shown = similarity.compare(conn, event["material_id"], target)
            result = analysis.latest_valid(conn, event_id)
        hypothesis = next((h for h in (result or {}).get("hypotheses", []) if h["horizon"] == "20d"
                           and h["metric"] == "outright_price"), None)
        instrument = _instrument(kept + shown, hypothesis)
        rows = []
        for state, group in (("kept", kept), ("overlap", [a for a in shown if a.case["status"] == "verified"]),
                             ("draft", [a for a in shown if a.case["status"] == "draft"])):
            for a in group:
                r = next((r for r in a.reactions if r.instrument_id == instrument), None)
                h = HORIZON_20.get(r.frequency, 1) if r else 20
                rows.append({**_case(a.case, a.reactions, state == "overlap"), "points": a.points, "state": state,
                             "own_20": r.own.get(h) if r else None})
        values = [r["own_20"] for r in rows if r["state"] == "kept" and r["own_20"] is not None]
        band = ({"low": round(reactions.quantile(values, 0.1), 2), "high": round(reactions.quantile(values, 0.9), 2),
                 "median": round(reactions.quantile(values, 0.5), 2)} if len(values) >= similarity.MIN_CASES else None)
        estimate = ({"low": hypothesis["move_p10"], "high": hypothesis["move_p90"], "unit": hypothesis["move_unit"]}
                    if hypothesis and hypothesis["instrument_id"] == instrument else None)
        return {
            "event": {"id": event["id"], "title": event["title"], "type": event["event_type"],
                      "first_seen": event["first_seen_at"]},
            "summary": similarity.summary_line(kept), "instrument_id": instrument, "rows": rows, "band": band,
            "estimate": estimate,
            "estimate_outside": bool(band and estimate and (estimate["low"] < band["low"]
                                                             or estimate["high"] > band["high"])),
            "threshold": similarity.THRESHOLD, "as_of": date.fromisoformat(event["first_seen_at"][:10]).isoformat(),
        }

    return router
