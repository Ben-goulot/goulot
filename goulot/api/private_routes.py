"""Routes of Ma chaîne, supplier signals, quotes, purchases and the « Ma matière » column (phase 4).

Everything here is under /api/private/: the middleware requires a session, the CSRF token on writes and a vault
token obtained by passkey (Touch ID). Responses are never cached (`Cache-Control: no-store`). The CSV import is a
JSON body read in memory, at most 1 MB, never written to disk. Nothing here is logged beyond method, path and status.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from datetime import date as Date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from goulot.api.context import AppContext
from goulot.chain import lifecycle, store
from goulot.collect.runner import freshness
from goulot.ledger import chain as ledger_chain
from goulot.ledger import personal as ledger_personal
from goulot.pipeline import assess
from goulot.private import cities, coverage, matching, records
from goulot.private import refresh as private_refresh
from goulot.private import service as private_service
from goulot.private.mychain import (
    CSV_TEMPLATE,
    ROLE_LABELS,
    WATCH_LABELS,
    Model,
    MyChain,
    check,
    market_concentration,
    my_concentration,
    parse_csv,
)
from goulot.private.personal import SIGNAL_TYPES, active_signals, expiring
from goulot.vault.keys import VaultError

UUID_RE = re.compile(r"^[0-9a-f-]{36}$")
MAX_CSV_CHARS = 1_000_000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeleteBody(_Strict):
    name: str = Field(max_length=300)


class CsvBody(_Strict):
    csv: str = Field(min_length=1, max_length=MAX_CSV_CHARS)


class SignalBody(_Strict):
    type: Literal["delay", "price_increase", "allocation", "force_majeure", "rupture", "back_to_normal"]
    site_id: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,40}$")
    date: Date
    effective_date: Date | None = None
    pct: float | None = Field(default=None, ge=-100, le=1000)
    old_lead_days: float | None = Field(default=None, ge=0, le=730)
    new_lead_days: float | None = Field(default=None, ge=0, le=730)
    comment: str = Field(default="", max_length=500)


class PriceBody(_Strict):
    date: Date
    unit_price: float = Field(gt=0, le=1e9)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    unit: str = Field(default="t", max_length=12)
    quantity: float | None = Field(default=None, gt=0, le=1e9)
    site_id: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,40}$")


class FeedbackBody(_Strict):
    seq: int = Field(ge=1)
    answer: Literal["tension", "none"]


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter(prefix="/api")

    def now() -> datetime:
        return datetime.fromtimestamp(ctx.clock.time(), UTC)

    def material(material_id: str) -> dict[str, Any]:
        if not UUID_RE.match(material_id):
            raise HTTPException(404, "Introuvable.")
        with ctx.db.connect() as conn:
            m = store.get_material(conn, material_id)
        if m is None:
            raise HTTPException(404, "Matière introuvable.")
        return m

    def recompute() -> None:
        """Links and personal verdicts right after a change, as the next watch cycle would."""
        at = now()
        fresh = freshness(ctx.db, at)
        with ctx.db.connect() as conn:
            models = private_service.safe_models(conn, ctx.vault)
        if models:
            with ctx.db.transaction() as conn:
                matching.refresh(conn, ctx.vault, models, at)
        if private_service.usable(ctx.vault):
            private_refresh.refresh(ctx.db, ctx.vault, at, models, verified=fresh["verified"],
                                    last_ok_cycle=fresh["last_ok_cycle"], break_in=assess.in_break_in(ctx.db, at))

    def guarded(fn: Any) -> Any:
        try:
            return fn()
        except VaultError:
            raise HTTPException(503, "Clé du coffre indisponible.") from None

    # --- public: the CSV template (no data) ------------------------------------------------------------------
    @router.get("/my-chain/template.csv")
    def template() -> PlainTextResponse:
        return PlainTextResponse("\ufeff" + CSV_TEMPLATE, media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": 'attachment; filename="ma_chaine_modele.csv"'})

    # --- Ma chaîne --------------------------------------------------------------------------------------------
    def describe(material_id: str, doc: MyChain) -> dict[str, Any]:
        with ctx.db.connect() as conn:
            typical = private_service.typical(conn, material_id)
        result = check(doc, typical)
        out: dict[str, Any] = {"check": {"errors": result.errors, "warnings": result.warnings}}
        if result.ok and doc.sites:
            model = Model(doc, typical)
            out["flows"] = model.link_flows()
            out["sites"] = {s.id: {"exposure_pct": round(100 * model.exposure.get(s.id, 0.0), 1),
                                   "weighs": model.weighs(s.id), "unexplained_pct": round(
                                       100 * model.unexplained(s.id), 1)} for s in doc.sites}
            out["concentration"] = {"market": market_concentration(model), "mine": my_concentration(model)}
        return out

    @router.get("/private/materials/{material_id}/my-chain")
    def my_chain(material_id: str) -> dict[str, Any]:
        material(material_id)

        def read() -> dict[str, Any]:
            with ctx.db.connect() as conn:
                found = private_service.latest(conn, ctx.vault, material_id)
                history = private_service.revisions(conn, ctx.vault, material_id)
            doc = found[1] if found else MyChain()
            return {"revision": found[0] if found else 0, "revisions": history, "chain": doc.model_dump(mode="json"),
                    **describe(material_id, doc), "roles": ROLE_LABELS, "watch_levels": WATCH_LABELS,
                    "cities_available": cities.available(), "attribution": cities.ATTRIBUTION,
                    "ready": ctx.vault.recovery_confirmed()}

        return guarded(read)  # type: ignore[no-any-return]

    @router.post("/private/materials/{material_id}/my-chain/check")
    def my_chain_check(material_id: str, body: MyChain) -> dict[str, Any]:
        material(material_id)
        return describe(material_id, body)

    @router.put("/private/materials/{material_id}/my-chain")
    def my_chain_save(material_id: str, body: MyChain) -> dict[str, Any]:
        material(material_id)
        try:
            revision, result = private_service.save(ctx.db, ctx.vault, material_id, body)
        except private_service.NotReady as exc:
            raise HTTPException(409, str(exc)) from None
        except VaultError:
            raise HTTPException(503, "Clé du coffre indisponible.") from None
        if revision:
            recompute()
        return {"revision": revision, "errors": result.errors, "warnings": result.warnings}

    @router.delete("/private/materials/{material_id}/my-chain")
    def my_chain_erase(material_id: str) -> dict[str, int]:
        material(material_id)
        removed = guarded(lambda: records.erase_material(ctx.db, ctx.vault, material_id))
        with ctx.db.transaction() as conn:
            conn.execute("UPDATE verdicts SET current = 0 WHERE material_id = ? AND scope = 'personal'",
                         (material_id,))
        recompute()
        return {"removed": int(removed)}

    @router.post("/private/materials/{material_id}/delete")
    def material_delete(material_id: str, body: DeleteBody) -> dict[str, bool]:
        """Final deletion (docs/SPEC.md, section 2): behind the vault, so that Ma chaîne, signals, quotes, purchases
        and coverage of the material are erased with it, whether or not they exist. The ledger is never touched."""
        material(material_id)
        guarded(lambda: records.erase_material(ctx.db, ctx.vault, material_id))
        try:
            with ctx.db.transaction() as conn:
                lifecycle.delete(conn, material_id, body.name)
        except lifecycle.LifecycleError as exc:
            raise HTTPException(409, str(exc)) from None
        with ctx.db.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # no copy of the deleted pages left in the WAL
        recompute()
        return {"ok": True}

    @router.post("/private/materials/{material_id}/my-chain/import")
    def my_chain_import(material_id: str, body: CsvBody) -> dict[str, Any]:
        """Preview only: the user checks the result, then saves it like any edit."""
        material(material_id)
        doc, errors = parse_csv(body.csv)
        if doc is None:
            return {"chain": None, "errors": errors}
        return {"chain": doc.model_dump(mode="json"), "errors": [], **describe(material_id, doc)}

    @router.get("/private/cities")
    def city_search(q: str = "", country: str = "") -> dict[str, Any]:
        if len(q) > 60 or (country and not re.fullmatch(r"[A-Z]{2}", country)):
            raise HTTPException(422, "Données invalides.")
        return {"available": cities.available(), "results": cities.search(q, country or None),
                "attribution": cities.ATTRIBUTION}

    # --- personal verdict and private records -----------------------------------------------------------------
    @router.get("/private/materials/{material_id}/personal")
    def personal(material_id: str) -> dict[str, Any]:
        material(material_id)

        def read() -> dict[str, Any]:
            today = now().date()
            with ctx.db.connect() as conn:
                verdict = private_refresh.current(conn, ctx.vault, material_id)
                found = private_service.latest(conn, ctx.vault, material_id)
                names = {s.id: s.name for s in found[1].sites} if found else {}
                signals = records.read_all(conn, ctx.vault, "signal", material_id)
                live = {s["id"] for s in active_signals(signals, today)}
                late = {s["id"] for s in expiring(signals, today)}
                links = records.read_links(conn, ctx.vault, material_id) if found else {}
                private_events = [dict(r) for r in conn.execute(
                    "SELECT id, title, place, event_status, confirmation, first_seen_at, lat, lon FROM events WHERE "
                    "material_id = ? AND id NOT IN (SELECT id FROM public_events)", (material_id,))
                    if r["id"] in links]
                quotes = records.read_all(conn, ctx.vault, "quote", material_id)
                purchases = records.read_all(conn, ctx.vault, "purchase", material_id)
                questions = ledger_personal.questions(conn, ctx.vault, today, material_id)
            cover = next((c for c in coverage.list_all(ctx.db, ctx.vault) if c["material_ref"] == material_id), None)
            return {
                "verdict": verdict, "coverage": cover,
                "links": {k: {**v, "site_names": [names.get(s, s) for s in v["sites"]]} for k, v in links.items()},
                "private_events": private_events,
                "signals": [{**s, "label": SIGNAL_TYPES.get(s["type"], s["type"]), "site": names.get(
                    s.get("site_id") or "", ""), "active": s["id"] in live, "expired": s["id"] in late}
                    for s in signals],
                "quotes": quotes, "purchases": purchases, "questions": questions,
            }

        return guarded(read)  # type: ignore[no-any-return]

    @router.post("/private/materials/{material_id}/signals")
    def add_signal(material_id: str, body: SignalBody) -> dict[str, str]:
        material(material_id)
        payload = {"material_id": material_id, **body.model_dump(mode="json")}
        signal_id = guarded(lambda: records.add(ctx.db, ctx.vault, "signal", payload))
        with ctx.db.transaction() as conn:
            ledger_personal.record_signal(conn, ctx.vault, material_id, {**payload, "id": signal_id}, at=now())
        recompute()
        return {"id": str(signal_id)}

    def add_price(kind: str, material_id: str, body: PriceBody) -> dict[str, str]:
        material(material_id)
        payload = {"material_id": material_id, **body.model_dump(mode="json")}
        record_id = guarded(lambda: records.add(ctx.db, ctx.vault, kind, payload))
        return {"id": str(record_id)}

    @router.post("/private/materials/{material_id}/quotes")
    def add_quote(material_id: str, body: PriceBody) -> dict[str, str]:
        return add_price("quote", material_id, body)

    @router.post("/private/materials/{material_id}/purchases")
    def add_purchase(material_id: str, body: PriceBody) -> dict[str, str]:
        return add_price("purchase", material_id, body)

    @router.delete("/private/materials/{material_id}/{kind}/{record_id}")
    def delete_record(material_id: str, kind: str, record_id: str) -> dict[str, bool]:
        material(material_id)
        kinds = {"quotes": "quote", "purchases": "purchase", "signals": "signal"}
        if kind not in kinds or not UUID_RE.match(record_id):
            raise HTTPException(404, "Introuvable.")
        ok = guarded(lambda: records.delete(ctx.db, ctx.vault, kinds[kind], record_id, material_id))
        if not ok:
            raise HTTPException(404, "Introuvable.")
        if kind == "signals":
            recompute()
        return {"ok": True}

    @router.post("/private/feedback")
    def feedback(body: FeedbackBody) -> dict[str, bool]:
        with ctx.db.transaction() as conn:
            entry = next((e for e in ledger_chain.entries(conn, kinds=("verdict",), scope="personal")
                          if e["seq"] == body.seq), None)
            if entry is None:
                raise HTTPException(404, "Conseil introuvable.")
            ledger_personal.record_feedback(conn, ctx.vault, entry["material_id"], body.seq, body.answer, at=now())
        return {"ok": True}

    @router.get("/private/reliability")
    def reliability(material_id: str | None = None) -> dict[str, Any]:
        if material_id is not None:
            material(material_id)

        def read() -> dict[str, Any]:
            today = now().date()
            with ctx.db.connect() as conn:
                return {"indicators": ledger_personal.indicators(conn, ctx.vault, today, material_id),
                        "entries": ledger_personal.entries(conn, ctx.vault, material_id)[-200:],
                        "questions": ledger_personal.questions(conn, ctx.vault, today, material_id)}

        return guarded(read)  # type: ignore[no-any-return]

    return router


def locked_summary(ctx: AppContext, material_id: str) -> dict[str, Any] | None:
    """What a closed vault may show of a personal verdict: its label (without X) and the action window, with
    « motif dans le coffre ». Used by the public routes; nothing else of Ma chaîne leaves through them."""
    with ctx.db.connect() as conn:
        row = conn.execute("SELECT verdict FROM verdicts WHERE material_id = ? AND scope = 'personal' "
                           "AND current = 1", (material_id,)).fetchone()
        if row is None:
            return None
        window = None
        if private_service.usable(ctx.vault):
            try:
                window = (private_refresh.current(conn, ctx.vault, material_id) or {}).get("window_weeks")
            except VaultError:
                window = None
    return {"verdict": row["verdict"], "label": private_refresh.locked_label(row["verdict"]),
            "window_weeks": window if row["verdict"] in ("ant", "pot") else None, "reason": "motif dans le coffre"}
