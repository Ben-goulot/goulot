"""Step 2, call 1: free web research, cited (docs/SPEC.md, section 3).

The code replaces each web-search citation with an `[srcN]` marker and keeps URL, title, publisher and date in
`sources`. URLs typed by the model in its prose are removed: the structuring call never sees nor writes a URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from goulot.llm.backend import MessageBackend
from goulot.llm.budget import Budget
from goulot.llm.calls import CallSpec, LLMError, call
from goulot.llm.prompts import load_prompt, material_block

PROMPT = "chain_research_v1"
MODEL = "claude-sonnet-5-5"
WEB_SEARCH_TOOL = "web_search_20260318"
MAX_SEARCHES = 20
MAX_TOKENS = 32_000
URL_IN_TEXT = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
SRC_MARKER = re.compile(r"\[src\d+\]")


@dataclass
class ResearchResult:
    report: str  # French report, citations as [srcN]
    sources: list[dict[str, str]] = field(default_factory=list)  # ref, url, title, publisher, page_age
    cost_usd: float = 0.0
    web_searches: int = 0


def research_params(material: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": load_prompt(PROMPT),
        # Direct calls: with dynamic filtering (the default) results reach the model through code output and the
        # report comes back without any citation, so without sources (verified on 2026-10-16).
        "tools": [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": MAX_SEARCHES,
                   "allowed_callers": ["direct"]}],
        "messages": [{"role": "user", "content": material_block(material)}],
    }


def _publisher(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def _safe_url(url: Any) -> str | None:
    if not isinstance(url, str) or len(url) > 2000:
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return None
    return url


def build_report(content: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """Report text with [srcN] markers, and the source table, from the response content blocks."""
    page_ages: dict[str, str] = {}
    for block in content:
        if block.get("type") == "web_search_tool_result" and isinstance(block.get("content"), list):
            for result in block["content"]:
                url = _safe_url(result.get("url"))
                if url and result.get("page_age"):
                    page_ages[url] = str(result["page_age"])[:40]
    refs: dict[str, str] = {}
    sources: list[dict[str, str]] = []
    parts: list[str] = []
    for block in content:
        if block.get("type") != "text":
            continue
        text = SRC_MARKER.sub("", URL_IN_TEXT.sub("[lien retiré]", str(block.get("text") or "")))
        markers: list[str] = []
        for citation in block.get("citations") or []:
            url = _safe_url(citation.get("url"))
            if url is None:
                continue
            if url not in refs:
                refs[url] = f"src{len(refs) + 1}"
                sources.append({
                    "ref": refs[url],
                    "url": url,
                    "title": str(citation.get("title") or "")[:300],
                    "publisher": _publisher(url),
                    "page_age": page_ages.get(url, ""),
                })
            if refs[url] not in markers:
                markers.append(refs[url])
        parts.append(text + "".join(f" [{m}]" for m in markers))
    return "".join(parts).strip(), sources


def run_research(
    backend: MessageBackend, budget: Budget, material: dict[str, Any], *, budget_name: str, max_cost_usd: float,
    material_id: str | None = None,
) -> ResearchResult:
    spec = CallSpec(
        key=f"chain_research_{material['cas']}", purpose="chain_research", budget=budget_name,
        max_cost_usd=max_cost_usd, material_id=material_id,
    )
    result = call(backend, budget, spec, research_params(material))
    report, sources = build_report(result.content)
    if not sources:  # a chain without any source is unusable: do not pay for the structuring call
        raise LLMError("La recherche n'a renvoyé aucune source citée : chaîne non structurée.")
    return ResearchResult(report, sources, result.cost_usd, result.usage.web_searches)
