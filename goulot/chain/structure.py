"""Step 2, calls 2 and 3: structured graph from the research report, without tools (docs/SPEC.md, section 3).

The API refuses one schema holding the whole chain (« compiled grammar is too large », 2026-10-16), so:
- call 2 (`chain_structure_v2`, `supply_chain_core_v1`): material, nodes, edges. Required; a schema failure is
  retried once.
- call 3 (`chain_context_v1`, `supply_chain_context_v1`): routes, substitutes, risks, dates, given the node list.
  Optional: if it fails, the chain is kept and the gap is shown as an open question.
Both outputs are merged into the supply_chain shape and checked by code; anomalies drop a node or edge to low
confidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from goulot.llm.backend import MessageBackend
from goulot.llm.budget import Budget, BudgetExceeded, CircuitOpen
from goulot.llm.calls import CallSpec, LLMError, call
from goulot.llm.prompts import escape, load_prompt, material_block, sources_block, tag
from goulot.llm.validate import ChainCheck, SchemaError, check_chain, parse_json_output, schema, validate_schema

log = logging.getLogger("goulot.chain.structure")

PROMPT = "chain_structure_v2"
SCHEMA = "supply_chain_core_v1"
CONTEXT_PROMPT = "chain_context_v1"
CONTEXT_SCHEMA = "supply_chain_context_v1"
MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 32_000
CONTEXT_MAX_TOKENS = 16_000
MAX_ATTEMPTS = 2  # core call only
CONTEXT_MISSING = "Voies de production, substituts, risques et échéances non structurés : à compléter à la relecture."


class StructureError(LLMError):
    pass


@dataclass
class StructureResult:
    chain: dict[str, Any]
    check: ChainCheck
    cost_usd: float
    attempts: int


def _params(prompt: str, schema_name: str, max_tokens: int, user: str) -> dict[str, Any]:
    return {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": load_prompt(prompt), "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": schema(schema_name)}},
        "messages": [{"role": "user", "content": user}],
    }


def _inputs(material: dict[str, Any], report: str, sources: list[dict[str, str]]) -> list[str]:
    return [material_block(material), tag("research_report", escape(report)), sources_block(sources)]


def structure_params(material: dict[str, Any], report: str, sources: list[dict[str, str]]) -> dict[str, Any]:
    return _params(PROMPT, SCHEMA, MAX_TOKENS, "\n\n".join(_inputs(material, report, sources)))


def context_params(material: dict[str, Any], report: str, sources: list[dict[str, str]],
                   nodes: list[dict[str, Any]]) -> dict[str, Any]:
    lines = "\n".join(f"{escape(str(n['node_id']))} | {escape(str(n['node_type']))} | {escape(str(n['name']))}"
                      for n in nodes)
    user = "\n\n".join([*_inputs(material, report, sources), tag("chain_nodes", lines)])
    return _params(CONTEXT_PROMPT, CONTEXT_SCHEMA, CONTEXT_MAX_TOKENS, user)


def detach_purchased_coproducts(chain: dict[str, Any]) -> set[str]:
    """Coproducts of the purchased material are not supply links: the graph must end at the purchased material.
    Edges leaving it with `yields_coproduct` are removed, with the coproduct nodes left without any link; their
    names go to the purchased material's notes. Returns the identifiers of the nodes removed."""
    tier0 = [n for n in chain["nodes"] if n["tier"] == 0]
    if len(tier0) != 1:
        return set()
    sink = tier0[0]
    leaving = [e for e in chain["edges"] if e["from_id"] == sink["node_id"] and e["relation"] == "yields_coproduct"]
    if not leaving:
        return set()
    chain["edges"] = [e for e in chain["edges"] if e not in leaving]
    linked = {e["from_id"] for e in chain["edges"]} | {e["to_id"] for e in chain["edges"]}
    targets = {e["to_id"] for e in leaving}
    removed = {n["node_id"] for n in chain["nodes"] if n["node_id"] in targets and n["node_id"] not in linked}
    names = [n["name"] for n in chain["nodes"] if n["node_id"] in removed]
    chain["nodes"] = [n for n in chain["nodes"] if n["node_id"] not in removed]
    if names:
        note = "Coproduits de la fabrication : " + ", ".join(names) + "."
        sink["notes"] = f"{sink['notes']} {note}".strip()
    return removed


def run_structure(
    backend: MessageBackend, budget: Budget, material: dict[str, Any], report: str, sources: list[dict[str, str]],
    *, budget_name: str, max_cost_usd: float, context_max_cost_usd: float = 0.3, material_id: str | None = None,
) -> StructureResult:
    allowed = {s["ref"] for s in sources}
    cas = str(material["cas"])
    cost = 0.0
    core: dict[str, Any] | None = None
    attempts = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        key = f"chain_structure_{cas}" + ("" if attempt == 1 else f"_retry{attempt - 1}")
        spec = CallSpec(key=key, purpose="chain_structure", budget=budget_name,
                        max_cost_usd=max_cost_usd, material_id=material_id)
        result = call(backend, budget, spec, structure_params(material, report, sources))
        cost += result.cost_usd
        try:
            core = validate_schema(SCHEMA, parse_json_output(result.content))
            break
        except SchemaError:
            continue
    if core is None:
        raise StructureError("La sortie de l'IA ne respecte pas le schéma, même après un nouvel essai.")

    removed = detach_purchased_coproducts(core)  # before the context call, which then sees the clean node list
    chain: dict[str, Any] = {**core, "routes": [], "substitutes": [], "structural_risks": [], "upcoming_dates": []}
    spec = CallSpec(key=f"chain_context_{cas}", purpose="chain_context", budget=budget_name,
                    max_cost_usd=context_max_cost_usd, material_id=material_id)
    try:
        result = call(backend, budget, spec, context_params(material, report, sources, core["nodes"]))
        cost += result.cost_usd
        context = validate_schema(CONTEXT_SCHEMA, parse_json_output(result.content))
        for part in ("routes", "substitutes", "structural_risks", "upcoming_dates"):
            for item in context[part]:
                if "node_ids" in item:
                    item["node_ids"] = [nid for nid in item["node_ids"] if nid not in removed]
            chain[part] = context[part]
        chain["open_questions"] = [*core["open_questions"], *context["open_questions"]]
        chain["injection_suspected"] = bool(core["injection_suspected"] or context["injection_suspected"])
    except (LLMError, SchemaError, BudgetExceeded, CircuitOpen) as exc:
        log.warning("context call skipped: %s", type(exc).__name__)
        chain["open_questions"] = [*core["open_questions"], CONTEXT_MISSING]

    check = check_chain(chain, allowed, cas)
    if check.fatal:
        raise StructureError("Chaîne inutilisable : " + " ; ".join(check.fatal) + ".")
    return StructureResult(chain, check, round(cost, 6), attempts)
