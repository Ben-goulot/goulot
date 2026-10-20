"""Routes of « Marchés », « Fiche matière » and « Fiabilité » (phase 3). Public data only: Ma chaîne, signals and
the personal verdict are under /api/private/; a closed vault shows only the label of the personal verdict and its
action window (`locked_summary`). Events found only through Ma chaîne never appear here (`public_events`). Model
texts are returned as strings, displayed as text and clipped by the page."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from goulot.api.context import AppContext
from goulot.api.private_routes import locked_summary
from goulot.chain import store
from goulot.collect.runner import freshness
from goulot.db.connection import set_setting
from goulot.ledger import chain, outcomes
from goulot.llm.budget import BudgetExceeded, raise_headroom, watch_mode
from goulot.pipeline import analysis
from goulot.pipeline.verdict import LABELS, PRESSURE_LABELS
from goulot.pipeline.visibility import serious_event
from goulot.prices import margin
from goulot.prices import store as prices
from goulot.security.secrets import get_secret

UUID_RE = re.compile(r"^[0-9a-f-]{36}$")
INSTRUMENT_RE = re.compile(r"^[a-z_]+:[A-Za-z0-9_.=^|&\-]{1,80}$")
CODE_RE = re.compile(r"^[A-Za-z0-9_.=^|&\-]{1,80}$")
USER_SOURCES = {"fred": 1, "bls_ppi": 4, "eurostat": 4, "yahoo_chart": 6, "stooq": 6}
ROLE_RANK = {"direct": None, "official": 2, "index": 4, "input": None}
STATUS = {"confirmed_ongoing": ("En cours", "ongoing"), "preparing": ("En préparation", "preparing"),
          "early": ("Signal précoce", "early")}


class RaiseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extra_usd: float = Field(gt=0, le=15)


class ToggleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class LinkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(pattern=r"^(fred|bls_ppi|eurostat|yahoo_chart|stooq)$")
    code: str = Field(pattern=CODE_RE.pattern)
    role: str = Field(pattern=r"^(direct|official|index|input)$")
    name: str = Field(min_length=1, max_length=120)
    node_key: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,80}$")


def _status(e: dict[str, Any]) -> tuple[str, str]:
    if e["confirmation"] != "confirmed":
        return STATUS["early"]
    if e["event_status"] in ("threatened", "announced"):
        return STATUS["preparing"]
    return STATUS["confirmed_ongoing"]


def _verdict(conn: Any, material_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM verdicts WHERE material_id = ? AND scope = 'market' AND current = 1",
                       (material_id,)).fetchone()
    if row is None:
        return None
    v = {k: row[k] for k in ("verdict", "pressure", "x_weeks", "x_low", "x_high", "window_weeks", "window_assumed",
                             "rules_only", "break_in", "explanation", "created_at")}
    v["label"] = (f"Problème potentiel à {row['x_weeks']:.0f} sem." if row["verdict"] == "pot" and row["x_weeks"]
                  else LABELS[row["verdict"]])
    v["pressure_label"] = PRESSURE_LABELS[row["pressure"]]
    v["event_ids"] = json.loads(row["event_ids"])
    return v


def _series_payload(s: Any, months: int) -> dict[str, Any] | None:
    if s is None:
        return None
    start = s.points[-1][0] - timedelta(days=30 * months)
    points = [{"day": d.isoformat(), "value": v} for d, v in s.points if d >= start]
    first = points[0]["value"] if points else None
    last = points[-1]["value"] if points else None
    return {"name": s.name, "rank": s.rank, "frequency": s.frequency, "source": s.source, "points": points,
            "change_pct": round(100 * (last / first - 1), 2) if first and last else None}


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
        return m

    @router.get("/markets")
    def markets() -> dict[str, Any]:
        fresh = freshness(ctx.db, now())
        out = []
        with ctx.db.connect() as conn:
            for m in store.list_materials(conn):
                best = prices.best_series(conn, m["id"]) if m["status"] == "validated" else None
                series = _series_payload(best, 2)
                out.append({**m, "verdict": _verdict(conn, m["id"]), "personal": locked_summary(ctx, m["id"]),
                            "series": {**series, "points": series["points"][-40:]} if series else None})
        return {"freshness": fresh, "materials": out}

    @router.get("/materials/{material_id}/sheet")
    def sheet(material_id: str) -> dict[str, Any]:
        today = now()
        with ctx.db.connect() as conn:
            m = material_or_404(conn, material_id)
            nodes = {n["node_key"]: n for n in store.load_chain(conn, material_id)["nodes"]}
            events = []
            rows = conn.execute("SELECT * FROM public_events WHERE material_id = ? AND last_seen_at >= ? "
                                "ORDER BY first_seen_at", (material_id,
                                                           (today - timedelta(days=60)).isoformat())).fetchall()
            for i, e in enumerate(rows):
                label, kind = _status(dict(e))
                links = [r[0] for r in conn.execute("SELECT node_key FROM event_links WHERE event_id = ?", (e["id"],))]
                result = analysis.latest_valid(conn, e["id"])
                hyp = next((h for h in (result or {}).get("hypotheses", []) if h["horizon"] == "20d"
                            and h["metric"] == "outright_price"), None)
                publishers = [r[0] for r in conn.execute(
                    "SELECT DISTINCT COALESCE(NULLIF(a.provider_domain, ''), a.domain) FROM event_articles ea "
                    "JOIN articles a ON a.id = ea.article_id WHERE ea.event_id = ? LIMIT 5", (e["id"],))]
                lat, lon = e["lat"], e["lon"]
                if lat is None:
                    located = [nodes[k] for k in links if k in nodes and nodes[k]["lat"] is not None]
                    lat, lon = (located[0]["lat"], located[0]["lon"]) if located else (None, None)
                events.append({
                    "id": e["id"], "letter": chr(ord("A") + i % 26), "title": e["title"], "place": e["place"],
                    "status_label": label, "status": kind, "c_fait": e["c_fait"], "lineages": e["lineages"],
                    "nodes": [nodes[k]["name"] for k in links if k in nodes], "publishers": publishers,
                    "first_seen": e["first_seen_at"], "lat": lat, "lon": lon,
                    "analysis_state": e["analysis_state"],
                    "impact": {"p10": hyp["move_p10"], "p90": hyp["move_p90"], "probability": hyp["probability"],
                               "unit": hyp["move_unit"]} if hyp else None,
                    "headline": (result or {}).get("headline", ""),
                    "future_dates": json.loads(e["future_dates"]),
                })
            latest = conn.execute("SELECT result FROM analyses a JOIN events e ON e.id = a.event_id "
                                  "WHERE e.material_id = ? AND a.state = 'done' ORDER BY a.done_at DESC LIMIT 1",
                                  (material_id,)).fetchone()
            card = json.loads(latest[0]) if latest else None
            best = prices.best_series(conn, material_id)
            margin_series = margin.for_material(conn, material_id)
            margin_trend = margin.trend(margin_series)
            series_links = [{"instrument_id": r["instrument_id"], "name": r["name"], "role": r["role"],
                             "rank": r["rank"], "validated": bool(r["validated"]), "proposed_by": r["proposed_by"]}
                            for r in prices.links(conn, material_id)]
            upcoming = [{"date": d["date"], "what": d["what"], "source": "chaîne"}
                        for d in store.load_chain(conn, material_id)["dates"]]
            upcoming += [{"date": d["date"], "what": d["what"], "source": e["letter"]} for e in events
                         for d in e["future_dates"]]
            this_month = today.date().isoformat()[:7]
            upcoming = sorted([u for u in upcoming if u["date"] >= this_month], key=lambda u: u["date"])[:12]
            sites = [{"name": n["name"], "lat": n["lat"], "lon": n["lon"], "type": n["node_type"]}
                     for n in nodes.values() if n["lat"] is not None]
            verdict = _verdict(conn, material_id)
        return {
            "material": m, "verdict": verdict, "personal": locked_summary(ctx, material_id),
            "freshness": freshness(ctx.db, today), "events": events,
            "analysis": {"headline": card.get("headline", ""), "summary": card.get("summary", ""),
                         "triggers": card.get("monitoring_triggers", []),
                         "confidence": card.get("confidence", {}).get("analysis_confidence")} if card else None,
            "series": _series_payload(best, 6), "links": series_links, "upcoming": upcoming, "sites": sites,
            "margin": {"trend": margin_trend[0], "change_pct_20": margin_trend[1],
                       "series": _series_payload(margin_series, 6)} if margin_trend else None,
        }

    @router.post("/materials/{material_id}/links")
    def add_link(material_id: str, body: LinkBody) -> dict[str, str]:
        with ctx.db.transaction() as conn:
            material_or_404(conn, material_id)
            rank = ROLE_RANK.get(body.role) or USER_SOURCES[body.source]
            if body.source in ("yahoo_chart", "stooq"):
                rank = 6  # fallback only
            frequency = "monthly" if body.source in ("bls_ppi", "eurostat") else "daily"
            iid = prices.ensure_instrument(conn, body.source, body.code, body.name, rank=rank, frequency=frequency)
            prices.link(conn, material_id, iid, body.role, proposed_by="user", validated=True, node_key=body.node_key)
        return {"instrument_id": iid}

    @router.post("/materials/{material_id}/links/{instrument_id}/validate")
    def validate_link(material_id: str, instrument_id: str) -> dict[str, bool]:
        if not INSTRUMENT_RE.match(instrument_id):
            raise HTTPException(404, "Introuvable.")
        with ctx.db.transaction() as conn:
            material_or_404(conn, material_id)
            cur = conn.execute("UPDATE instrument_links SET validated = 1 WHERE material_id = ? AND instrument_id = ?",
                               (material_id, instrument_id))
            if cur.rowcount == 0:
                raise HTTPException(404, "Série introuvable.")
        return {"ok": True}

    @router.get("/budget")
    def budget_status() -> dict[str, Any]:
        mode = watch_mode(ctx.budget)
        with ctx.db.connect() as conn:
            serious = serious_event(conn)
        return {"mode": mode.mode, "spent_usd": mode.spent, "cap_usd": mode.cap,
                "reconstruction_spent_usd": round(ctx.budget.spent("reconstruction"), 4),
                "reconstruction_cap_usd": ctx.budget.cap("reconstruction"), "serious_event": serious,
                "raise_headroom_usd": raise_headroom(ctx.budget) if serious else 0.0}

    @router.post("/budget/raise")
    def budget_raise(body: RaiseBody) -> dict[str, float]:
        with ctx.db.connect() as conn:
            serious = serious_event(conn)
        if not serious:
            raise HTTPException(409, "Le plafond ne se relève que pendant un événement grave en cours.")
        try:
            return {"cap_usd": ctx.budget.raise_watch_cap(body.extra_usd)}
        except BudgetExceeded as exc:
            raise HTTPException(409, str(exc)) from None

    @router.put("/settings/alerts-discreet")
    def alerts_discreet(body: ToggleBody) -> dict[str, bool]:
        with ctx.db.transaction() as conn:
            set_setting(conn, "alerts_discreet", "1" if body.enabled else "0")
        return {"enabled": body.enabled}

    @router.put("/settings/heartbeat")
    def heartbeat_toggle(body: ToggleBody) -> dict[str, bool]:
        if body.enabled and not get_secret("healthcheck"):
            raise HTTPException(409, "Adresse de l'alerte de vie absente : « ./bin/goulot setup ».")
        with ctx.db.transaction() as conn:
            set_setting(conn, "heartbeat_enabled", "1" if body.enabled else "0")
        return {"enabled": body.enabled}

    @router.post("/alerts/test")
    def alerts_test() -> dict[str, Any]:
        from goulot.pipeline.cycle import sender

        send = sender(ctx)
        if send is None:
            raise HTTPException(409, "Aucun canal d'alerte configuré : « ./bin/goulot setup ».")
        try:
            send("Goulot : message de test. Le canal d'alerte fonctionne.")
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}
        return {"ok": True}

    @router.get("/alerts")
    def alerts_list() -> list[dict[str, Any]]:
        with ctx.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT kind, text, created_at, sent_at, error FROM alerts ORDER BY id DESC LIMIT 50")]

    @router.get("/reliability")
    def reliability(material_id: str | None = None) -> dict[str, Any]:
        today = now().date()
        with ctx.db.connect() as conn:
            if material_id is not None:
                material_or_404(conn, material_id)
            check = chain.verify(conn)
            stats = outcomes.indicators(conn, today, material_id)
            entries = chain.entries(conn, material_id=material_id)
            by_seq = {e["seq"]: e for e in entries}
            outcome_of = {e["of_seq"]: e for e in entries if e["kind"] == "outcome"}
            advices = [{**e, "outcome": outcome_of.get(e["seq"])} for e in entries
                       if e["kind"] in ("verdict", "hypothesis", "pressure", "missed_move")]
            series = _series_payload(prices.best_series(conn, material_id), 12) if material_id else None
        return {
            "ledger": {"ok": check.ok, "entries": check.entries, "head": check.head[:16], "problem": check.problem,
                       "at_seq": check.at_seq},
            "indicators": stats, "advices": advices[-200:], "series": series,
            "known_entries": len(by_seq), "today": date.isoformat(today),
        }

    return router
