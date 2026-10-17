"""One entry point for every AI call (CLAUDE.md, rule 8): budget check, send, `pause_turn`, stop reasons,
cost recorded in `llm_calls`. Logs carry the request id, never the content."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic

from goulot.llm.backend import MessageBackend
from goulot.llm.budget import Budget, CallRecord
from goulot.llm.pricing import Usage, cost_usd

log = logging.getLogger("goulot.llm")

MAX_CONTINUATIONS = 4  # `pause_turn` resumptions of a server-tool turn


class LLMError(RuntimeError):
    """The call did not produce a usable answer (message in French, for the screen)."""


class LLMRefused(LLMError):
    pass


class LLMTruncated(LLMError):
    pass


@dataclass
class CallResult:
    content: list[dict[str, Any]]
    usage: Usage
    cost_usd: float
    request_ids: list[str] = field(default_factory=list)
    stop_reason: str = ""


@dataclass(frozen=True)
class CallSpec:
    key: str  # recording key, e.g. "chain_research_107-13-1"
    purpose: str  # chain_research, chain_structure…
    budget: str
    max_cost_usd: float  # worst case, checked against the cap before sending
    material_id: str | None = None
    batch: bool = False


def call(backend: MessageBackend, budget: Budget, spec: CallSpec, params: dict[str, Any]) -> CallResult:
    budget.check(spec.budget, spec.max_cost_usd)
    model = str(params["model"])
    messages = list(params["messages"])
    content: list[dict[str, Any]] = []
    total = Usage()
    total_cost = 0.0
    request_ids: list[str] = []
    stop_reason = ""
    for round_ in range(MAX_CONTINUATIONS + 1):
        key = spec.key if round_ == 0 else f"{spec.key}_cont{round_}"
        try:
            response = backend.create(key, {**params, "messages": messages})
        except anthropic.APIError as exc:
            budget.record(CallRecord(spec.budget, spec.purpose, model, "standard", Usage(), 0.0,
                                     material_id=spec.material_id, stop_reason="error", ok=False))
            status = getattr(exc, "status_code", "")
            body = getattr(exc, "body", None)
            detail = body.get("error", {}) if isinstance(body, dict) else {}
            # API metadata only (status, error type and message), never the prompt nor the answer.
            api_message = str(detail.get("message", ""))[:300]
            log.warning("llm call failed: purpose=%s error=%s status=%s type=%s message=%s", spec.purpose,
                        type(exc).__name__, status, detail.get("type", ""), api_message)
            reason = f"HTTP {status}, {api_message}" if status else type(exc).__name__
            raise LLMError(f"L'appel à l'IA a échoué ({reason}).") from exc
        replayed = bool(response.get("_replayed"))  # already paid in an earlier run: not billed again
        usage = Usage() if replayed else Usage.from_api(response.get("usage"))
        cost = cost_usd(model, usage, batch=spec.batch)
        stop_reason = str(response.get("stop_reason") or "")
        request_id = str(response.get("_request_id") or response.get("id") or "")
        ok = stop_reason in ("end_turn", "pause_turn")
        if not replayed:
            budget.record(CallRecord(spec.budget, spec.purpose, model, "batch" if spec.batch else "standard", usage,
                                     cost, request_id=request_id, material_id=spec.material_id,
                                     stop_reason=stop_reason, ok=ok))
        log.info("llm call: purpose=%s request_id=%s stop=%s cost=%.4f replayed=%s", spec.purpose, request_id,
                 stop_reason, cost, replayed)
        total += usage
        total_cost += cost
        request_ids.append(request_id)
        blocks: list[dict[str, Any]] = list(response.get("content") or [])
        content.extend(blocks)
        if stop_reason != "pause_turn":
            break
        messages = [*messages, {"role": "assistant", "content": blocks}]
        container = response.get("container")
        if isinstance(container, dict) and container.get("id"):
            params = {**params, "container": container["id"]}  # resume in the same server-side environment
    else:
        raise LLMTruncated("La recherche n'a pas abouti dans le nombre de reprises autorisé.")
    if stop_reason == "refusal":
        raise LLMRefused("L'IA a refusé de traiter cette demande.")
    if stop_reason == "max_tokens":
        raise LLMTruncated("La réponse de l'IA a été coupée (limite de longueur).")
    if stop_reason != "end_turn":
        raise LLMError(f"Réponse inattendue de l'IA ({stop_reason or 'sans motif'}).")
    return CallResult(content, total, round(total_cost, 6), request_ids, stop_reason)
