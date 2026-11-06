"""Routes of the « Conformité » screen (docs/SPEC.md, section 10).

Public routes (session only) build the report on the typical chain, without anything private: mitigation choices
and coverage stay in the vault, so such a report is always a draft. Routes under /api/private/ (vault token) add the
mitigation choices, the coverage and, on request, Ma chaîne. An export is built in memory and sent back with its
annexes; only its fingerprint and an `export` ledger entry are kept. No AI call, no network call.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from goulot.api.context import AppContext
from goulot.chain import store as chain_store
from goulot.compliance import report as compliance_report
from goulot.compliance import store as compliance_store
from goulot.compliance import templates
from goulot.private import coverage
from goulot.private import service as private_service
from goulot.vault.keys import VaultError

UUID = r"^[0-9a-f-]{36}$"


class ReportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    material_ids: list[str] = Field(min_length=1, max_length=50)
    basis: Literal["typical", "my_chain"] = "typical"
    company: str = Field(default="", max_length=200)
    signatory_name: str = Field(default="", max_length=120)
    signatory_role: str = Field(default="", max_length=120)
    reviewed: bool = False


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter(prefix="/api")

    def now() -> datetime:
        return datetime.fromtimestamp(ctx.clock.time(), UTC)

    def request(body: ReportBody) -> compliance_report.Request:
        try:
            template = templates.get(body.template, ctx.settings.config_dir)
        except templates.TemplateError:
            raise HTTPException(422, "Modèle inconnu.") from None
        ids = list(dict.fromkeys(body.material_ids))
        with ctx.db.connect() as conn:
            for mid in ids:
                if not re.fullmatch(UUID, mid) or chain_store.get_material(conn, mid) is None:
                    raise HTTPException(404, "Matière introuvable.")
        return compliance_report.Request(
            template=template, material_ids=ids, basis=body.basis, company=body.company,
            signatory_name=body.signatory_name, signatory_role=body.signatory_role, reviewed=body.reviewed)

    def private_inputs(req: compliance_report.Request) -> compliance_report.PrivateInputs:
        try:
            profile = compliance_store.get_profile(ctx.db, ctx.vault)
            cover = {c["material_ref"]: c for c in coverage.list_all(ctx.db, ctx.vault)}
            models: dict[str, Any] = {}
            if req.basis == "my_chain":
                with ctx.db.connect() as conn:
                    for mid in req.material_ids:
                        found = private_service.model(conn, ctx.vault, mid)
                        if found is not None:
                            models[mid] = found[1]
        except VaultError:
            raise HTTPException(503, "Clé du coffre indisponible.") from None
        return compliance_report.PrivateInputs(
            measures={k: v.model_dump() for k, v in profile.measures.items()}, coverage=cover, my_chains=models)

    def build(req: compliance_report.Request, private: compliance_report.PrivateInputs | None) -> dict[str, Any]:
        with ctx.db.connect() as conn:
            return compliance_report.build(conn, req, now(), private)

    def export(req: compliance_report.Request, private: compliance_report.PrivateInputs | None) -> dict[str, Any]:
        at = now()
        with ctx.db.transaction() as conn:
            report = compliance_report.build(conn, req, at, private)
            report["ledger_seq"] = compliance_store.record(conn, report, at=at)
        stamp = at.strftime("%Y-%m-%d")
        name = f"goulot_{report['template']['id']}_{stamp}_{report['fingerprint'][:8]}"
        return {"report": report, "files": {
            "json": {"name": f"{name}.json", "content": compliance_report.annex_json(report)},
            "csv": {"name": f"{name}.csv", "content": compliance_report.annex_csv(report)}}}

    # --- public: typical chain, nothing private ----------------------------------------------------------------
    @router.get("/compliance/options")
    def options() -> dict[str, Any]:
        with ctx.db.connect() as conn:
            materials = [{"id": r["id"], "name": r["name"], "cas": r["cas"],
                          "validated_at": (r["validated_at"] or "")[:10] or None}
                         for r in conn.execute("SELECT id, name, cas, validated_at FROM materials "
                                               "WHERE status = 'validated' ORDER BY name")]
        return {
            "templates": [{"id": t["id"], "version": t["version"], "title": t["title"],
                           "subtitle": t.get("subtitle", "")} for t in templates.available(
                               ctx.settings.config_dir).values()],
            "materials": materials,
            "mitigations": [{"id": k, "label": v} for k, v in templates.MITIGATION_LABELS.items()],
        }

    def public(body: ReportBody) -> compliance_report.Request:
        if body.basis != "typical":
            raise HTTPException(403, "Ma chaîne exige le coffre ouvert.")
        return request(body)

    @router.post("/compliance/preview")
    def preview(body: ReportBody) -> dict[str, Any]:
        return build(public(body), None)

    @router.post("/compliance/export")
    def export_public(body: ReportBody) -> dict[str, Any]:
        return export(public(body), None)

    # --- vault open ----------------------------------------------------------------------------------------------
    @router.get("/private/compliance/profile")
    def profile_get() -> dict[str, Any]:
        try:
            return compliance_store.get_profile(ctx.db, ctx.vault).model_dump(mode="json")
        except VaultError:
            raise HTTPException(503, "Clé du coffre indisponible.") from None

    @router.put("/private/compliance/profile")
    def profile_put(body: compliance_store.Profile) -> dict[str, bool]:
        try:
            compliance_store.put_profile(ctx.db, ctx.vault, body)
        except VaultError:
            raise HTTPException(503, "Clé du coffre indisponible.") from None
        return {"ok": True}

    @router.post("/private/compliance/preview")
    def preview_private(body: ReportBody) -> dict[str, Any]:
        req = request(body)
        return build(req, private_inputs(req))

    @router.post("/private/compliance/export")
    def export_private(body: ReportBody) -> dict[str, Any]:
        req = request(body)
        return export(req, private_inputs(req))

    return router
