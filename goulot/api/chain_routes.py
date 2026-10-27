"""Routes of « Ajouter une matière » and « Chaîne » (phase 2). Session and CSRF are enforced by the middleware.

Public data only: the typical chain is not private (docs/SPEC.md, section 2). Model texts are returned as plain
strings and shown as text; URLs come only from the identification sources and the web-search citations.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx2
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from goulot.api.context import AppContext, MissingAPIKey
from goulot.chain import places, service, store
from goulot.chain.delays import delays_to_material
from goulot.chain.identify import IdentificationError, identify
from goulot.chain.quote import confirm_quote, reconstruction_quote, save_quote
from goulot.chain.store import ChainStateError
from goulot.db.connection import set_setting
from goulot.geo import cities
from goulot.llm.budget import BUDGETS
from goulot.security.secrets import get_secret

log = logging.getLogger("goulot.api.chain")

UUID_RE = re.compile(r"^[0-9a-f-]{36}$")
NODE_KEY_RE = re.compile(r"^[a-z0-9_]{1,80}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryBody(_Strict):
    query: str = Field(min_length=1, max_length=100)


class ReconstructBody(_Strict):
    quote_id: str = Field(min_length=36, max_length=36)


class NodeChanges(_Strict):
    name: str | None = Field(default=None, max_length=200)
    product: str | None = Field(default=None, max_length=200)
    country_iso2: str | None = Field(default=None, pattern=r"^([A-Z]{2}|unknown)$")
    place: str | None = Field(default=None, max_length=200)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    global_share_pct: float | None = Field(default=None, ge=0, le=100)
    buffer_days: float | None = Field(default=None, ge=0, le=365)
    capacity_text: str | None = Field(default=None, max_length=200)
    confidence: str | None = Field(default=None, pattern=r"^(high|medium|low)$")
    notes: str | None = Field(default=None, max_length=1000)


class EdgeChanges(_Strict):
    transport_mode: str | None = Field(default=None, pattern=r"^(sea|river|rail|road|pipeline|air|none|unknown)$")
    lead_time_days: float | None = Field(default=None, ge=0, le=365)
    share_pct: float | None = Field(default=None, ge=0, le=100)
    confidence: str | None = Field(default=None, pattern=r"^(high|medium|low)$")


class ZoneBody(_Strict):
    zone: str = Field(max_length=20)


class ValidateBody(_Strict):
    acknowledge_injection: bool = False


def _uuid(value: str) -> str:
    if not UUID_RE.match(value):
        raise HTTPException(404, "Introuvable.")
    return value


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter(prefix="/api")

    def _now() -> datetime:
        return datetime.fromtimestamp(ctx.clock.time(), UTC)

    def _material(material_id: str) -> dict[str, Any]:
        with ctx.db.connect() as conn:
            material = store.get_material(conn, _uuid(material_id))
        if material is None:
            raise HTTPException(404, "Matière introuvable.")
        return material

    def _identify(query: str) -> dict[str, Any]:
        client = ctx.http_client()
        try:
            return identify(client, query).as_dict()
        except IdentificationError as exc:
            raise HTTPException(422, str(exc)) from None
        except httpx2.HTTPError as exc:
            log.warning("identification failed: %s", type(exc).__name__)
            raise HTTPException(502, "PubChem ne répond pas : vérifiez la connexion et réessayez.") from None
        finally:
            client.close()

    @router.post("/materials/identify")
    def materials_identify(body: QueryBody) -> dict[str, Any]:
        return _identify(body.query)

    @router.post("/materials")
    def materials_add(body: QueryBody) -> dict[str, Any]:
        identity = _identify(body.query)  # identified again server side: nothing from the page is trusted
        with ctx.db.transaction() as conn:
            material_id = store.create_material(conn, identity)
        return _material(material_id)

    @router.get("/materials")
    def materials_list() -> list[dict[str, Any]]:
        with ctx.db.connect() as conn:
            return store.list_materials(conn)

    @router.get("/materials/{material_id}")
    def material_get(material_id: str) -> dict[str, Any]:
        return _material(material_id)

    @router.post("/materials/{material_id}/quote")
    def material_quote(material_id: str) -> dict[str, Any]:
        material = _material(material_id)
        if material["status"] in ("validated", "reconstructing"):
            raise HTTPException(409, "Cette chaîne est déjà validée ou en cours de reconstruction.")
        quote = reconstruction_quote()
        with ctx.db.transaction() as conn:
            quote_id = save_quote(conn, material["id"], quote, _now())
        return {
            "quote_id": quote_id, "estimate_usd": quote.estimate_usd, "max_usd": quote.max_usd,
            "searches_max": quote.searches_max, "remaining_usd": ctx.budget.remaining("reconstruction"),
            "cap_usd": ctx.budget.cap("reconstruction"),
        }

    @router.post("/materials/{material_id}/reconstruct")
    def material_reconstruct(material_id: str, body: ReconstructBody) -> dict[str, str]:
        material = _material(material_id)
        if material["status"] in ("validated", "reconstructing"):
            raise HTTPException(409, "Cette chaîne est déjà validée ou en cours de reconstruction.")
        with ctx.db.transaction() as conn:
            max_usd = confirm_quote(conn, body.quote_id, material["id"], _now())
        if max_usd is None:
            raise HTTPException(409, "Devis expiré ou déjà utilisé : demandez un nouveau devis.")
        if ctx.budget.remaining("reconstruction") < max_usd:
            raise HTTPException(402, "Plafond mensuel des reconstructions insuffisant pour ce devis.")
        try:
            backend = ctx.llm_backend()
        except MissingAPIKey as exc:
            raise HTTPException(409, str(exc)) from None
        comtrade = get_secret("comtrade")
        job_id = service.start_job(ctx.db, material["id"])

        def run() -> None:
            http = ctx.http_client()
            try:
                service.reconstruct(service.Deps(ctx.db, ctx.budget, backend, http, comtrade), material["id"], job_id)
            except Exception:
                log.exception("reconstruction job crashed")
            finally:
                http.close()

        ctx.jobs.submit(run)
        return {"job_id": job_id}

    @router.get("/jobs/{job_id}")
    def job_get(job_id: str) -> dict[str, Any]:
        job = service.job_status(ctx.db, _uuid(job_id))
        if job is None:
            raise HTTPException(404, "Introuvable.")
        return job

    @router.get("/materials/{material_id}/chain")
    def chain_get(material_id: str) -> dict[str, Any]:
        material = _material(material_id)
        with ctx.db.connect() as conn:
            chain = store.load_chain(conn, material["id"])
        delays = delays_to_material(chain["nodes"], chain["edges"])
        for n in chain["nodes"]:
            n["delay"] = delays.get(n["node_key"])
        return {"material": material, **chain}

    @router.patch("/materials/{material_id}/nodes/{node_key}")
    def node_patch(material_id: str, node_key: str, body: NodeChanges) -> dict[str, bool]:
        material = _material(material_id)
        if not NODE_KEY_RE.match(node_key):
            raise HTTPException(404, "Introuvable.")
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(422, "Aucune correction.")
        try:
            with ctx.db.transaction() as conn:
                store.correct_node(conn, material["id"], node_key, changes)
        except ChainStateError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"ok": True}

    @router.patch("/materials/{material_id}/edges/{edge_id}")
    def edge_patch(material_id: str, edge_id: int, body: EdgeChanges) -> dict[str, bool]:
        material = _material(material_id)
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(422, "Aucune correction.")
        try:
            with ctx.db.transaction() as conn:
                store.correct_edge(conn, material["id"], edge_id, changes)
        except ChainStateError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"ok": True}

    @router.post("/materials/{material_id}/validate")
    def material_validate(material_id: str, body: ValidateBody) -> dict[str, bool]:
        material = _material(material_id)
        try:
            with ctx.db.transaction() as conn:
                store.validate_material(conn, material["id"], acknowledge_injection=body.acknowledge_injection)
        except ChainStateError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"ok": True}

    @router.get("/places/cities")
    def city_search(q: str = "", country: str = "") -> dict[str, Any]:
        """Offline city list (GeoNames), to place a node of the typical chain: nothing leaves the Mac."""
        if len(q) > 60 or (country and not re.fullmatch(r"[A-Z]{2}", country)):
            raise HTTPException(422, "Données invalides.")
        return {"available": cities.available(), "results": cities.search(q, country or None),
                "attribution": cities.ATTRIBUTION}

    @router.get("/settings/delivery-zone")
    def delivery_zone_get() -> dict[str, Any]:
        with ctx.db.connect() as conn:
            return {"zone": places.delivery_zone(conn), "choices": places.DELIVERY_ZONES}

    @router.put("/settings/delivery-zone")
    def delivery_zone_put(body: ZoneBody) -> dict[str, Any]:
        """The probable route straits of every validated chain follow the delivery zone."""
        if body.zone not in places.DELIVERY_ZONES:
            raise HTTPException(422, "Zone inconnue.")
        with ctx.db.transaction() as conn:
            set_setting(conn, "delivery_zone", body.zone)
            for m in store.list_materials(conn):
                store.refresh_rules(conn, m["id"])
        return {"zone": body.zone}

    @router.get("/watch/status")
    def watch_status() -> dict[str, Any]:
        from goulot.collect.runner import freshness

        with ctx.db.connect() as conn:
            sources = [dict(r) for r in conn.execute(
                "SELECT source, family, MAX(started_at) AS last_run, "
                "MAX(CASE WHEN ok = 1 THEN started_at END) AS last_ok FROM collect_runs "
                "GROUP BY source ORDER BY source")]
        return {**freshness(ctx.db, _now()), "sources": sources}

    @router.get("/llm/spend")
    def llm_spend() -> dict[str, dict[str, float]]:
        return {b: {"spent_usd": round(ctx.budget.spent(b), 4), "cap_usd": ctx.budget.cap(b)}
                for b in BUDGETS if b != "dev"}

    return router
