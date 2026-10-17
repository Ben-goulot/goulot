"""Reconstruction of a typical chain, in 5 steps shown on the « Ajouter une matière » screen:
1 identification, 2 research, 3 structuring, 4 cross-check, 5 lead times (docs/SPEC.md, section 3).

The model sees only public fields of the material (`goulot.llm.prompts.MATERIAL_FIELDS`); its outputs are
validated by schema then by code, and nothing is watched before the user validates the chain.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx2

from goulot.chain import crosscheck, geo, leadtimes, store
from goulot.chain.quote import reconstruction_quote
from goulot.chain.research import run_research
from goulot.chain.structure import run_structure
from goulot.db.connection import Database, utcnow
from goulot.llm.backend import MessageBackend
from goulot.llm.budget import Budget, BudgetExceeded, CircuitOpen
from goulot.llm.calls import LLMError

log = logging.getLogger("goulot.chain")

USGS_PAGE = "https://www.usgs.gov/centers/national-minerals-information-center"
STEPS = {1: "Identification", 2: "Recherche sourcée", 3: "Structuration", 4: "Recoupement", 5: "Délais"}


@dataclass
class Deps:
    db: Database
    budget: Budget
    backend: MessageBackend
    http: httpx2.Client | None  # None: no cross-check (tests, offline)
    comtrade_key: str | None = None
    usgs_csv: str | None = None  # MCS world production table, when configured


def start_job(db: Database, material_id: str) -> str:
    job_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO jobs (id, material_id, kind, step, state, started_at) VALUES (?, ?, 'reconstruction', 1, "
            "'running', ?)",
            (job_id, material_id, utcnow()),
        )
        store.set_status(conn, material_id, "reconstructing")
    return job_id


def _step(db: Database, job_id: str, step: int, cost: float) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET step = ?, cost_usd = ? WHERE id = ?", (step, round(cost, 6), job_id))


def _finish(db: Database, job_id: str, material_id: str, *, ok: bool, message: str) -> float:
    """Closes the job; its cost is read back from `llm_calls`, failed attempts included."""
    with db.transaction() as conn:
        started = conn.execute("SELECT started_at FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        cost = float(conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE material_id = ? AND at >= ?", (material_id, started)
        ).fetchone()[0])
        conn.execute(
            "UPDATE jobs SET state = ?, message = ?, cost_usd = ?, finished_at = ? WHERE id = ?",
            ("done" if ok else "failed", message, round(cost, 6), utcnow(), job_id),
        )
        if not ok:
            material = store.get_material(conn, material_id)
            has_chain = conn.execute(
                "SELECT 1 FROM chain_nodes WHERE material_id = ? LIMIT 1", (material_id,)
            ).fetchone()
            if material and material["status"] == "reconstructing":
                store.set_status(conn, material_id, "review" if has_chain else "draft")
    return cost


def material_for_prompt(material: dict[str, Any]) -> dict[str, Any]:
    """Whitelist: the prompts never receive anything else from the material record."""
    return {"name": material["name"], "cas": material["cas"], "formula": material.get("formula", ""),
            "purchased_form": material["name"]}


def cross_check(deps: Deps, chain: dict[str, Any], material: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if deps.http is not None:
        year = datetime.now(UTC).year - 2  # latest year with complete declarations
        for hs in chain["material"]["hs_code_candidates"][:1]:
            try:
                ref = crosscheck.comtrade_export_shares(deps.http, str(hs), year, deps.comtrade_key)
            except httpx2.HTTPError:
                ref = None
            if ref:
                crosscheck.apply_reference(chain, ref, tier=1)
            else:
                notes.append(f"Recoupement UN Comtrade indisponible pour le code SH {hs}.")
    if deps.usgs_csv:
        for word in {material["name"].split()[0], *(n["product"].split()[0] for n in chain["nodes"] if n["product"])}:
            ref = crosscheck.usgs_production_shares(deps.usgs_csv, word, USGS_PAGE)
            if ref:
                crosscheck.apply_reference(chain, ref, product_word=word)
    if not geo.borders_available():
        notes.append("Frontières Natural Earth absentes : la position des sites n'a pas été contrôlée.")
    else:
        geo.check_coordinates(chain)
    return notes


def reconstruct(deps: Deps, material_id: str, job_id: str, *, budget_name: str = "reconstruction") -> float:
    """Runs steps 2 to 5 (identification is done when the material is added). Returns the cost in dollars."""
    with deps.db.connect() as conn:
        material = store.get_material(conn, material_id)
    if material is None:
        raise ValueError("unknown material")
    quote = reconstruction_quote()
    prompt_material = material_for_prompt(material)
    cost = 0.0
    try:
        _step(deps.db, job_id, 2, cost)
        found = run_research(deps.backend, deps.budget, prompt_material, budget_name=budget_name,
                             max_cost_usd=quote.research_max_usd, material_id=material_id)
        cost += found.cost_usd
        _step(deps.db, job_id, 3, cost)
        structured = run_structure(deps.backend, deps.budget, prompt_material, found.report, found.sources,
                                   budget_name=budget_name, max_cost_usd=quote.structure_max_usd,
                                   context_max_cost_usd=quote.context_max_usd, material_id=material_id)
        cost += structured.cost_usd
        chain = structured.chain
        _step(deps.db, job_id, 4, cost)
        notes = cross_check(deps, chain, material)
        _step(deps.db, job_id, 5, cost)
        leadtimes.fill_defaults(chain)
        with deps.db.transaction() as conn:
            store.save_chain(conn, material_id, chain, found.sources, found.report,
                             node_anomalies=structured.check.nodes, edge_anomalies=structured.check.edges,
                             general=[*structured.check.general, *notes])
    except (LLMError, BudgetExceeded, CircuitOpen) as exc:
        return _finish(deps.db, job_id, material_id, ok=False, message=str(exc))
    except Exception:
        log.exception("reconstruction failed")
        _finish(deps.db, job_id, material_id, ok=False, message="Erreur interne pendant la reconstruction.")
        raise
    message = "Chaîne prête à relire."
    if chain["injection_suspected"]:
        message += " Contenu piégé signalé : à vérifier."
    return _finish(deps.db, job_id, material_id, ok=True, message=message)


def job_status(db: Database, job_id: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["step_label"] = STEPS.get(job["step"], "")
    return job
