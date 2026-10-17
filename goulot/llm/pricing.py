"""Cost of an AI call from the billed usage and `config/pricing.yaml` (CLAUDE.md, Budget)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from goulot.settings import CONFIG_DIR

PER_MILLION = 1_000_000


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0  # written with a 1-hour TTL (the only TTL Goulot uses)
    cache_read_tokens: int = 0
    web_searches: int = 0

    @classmethod
    def from_api(cls, usage: dict[str, Any] | None) -> Usage:
        usage = usage or {}
        server = usage.get("server_tool_use") or {}
        return cls(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            web_searches=int(server.get("web_search_requests") or 0),
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.web_searches + other.web_searches,
        )

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


@lru_cache(maxsize=4)
def load_pricing(path: Path = CONFIG_DIR / "pricing.yaml") -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def _rates(model: str, prompt_tokens: int, pricing: dict[str, Any]) -> dict[str, float]:
    try:
        rates: dict[str, Any] = pricing["models"][model]
    except KeyError:
        raise ValueError(f"no pricing for model {model}") from None
    threshold = rates.get("long_prompt_threshold_tokens")
    if threshold is not None and prompt_tokens > int(threshold):
        rates = rates["long_prompt"]
    return {k: float(v) for k, v in rates.items() if isinstance(v, int | float)}


def cost_usd(model: str, usage: Usage, *, batch: bool = False, pricing: dict[str, Any] | None = None) -> float:
    pricing = pricing or load_pricing()
    rates = _rates(model, usage.prompt_tokens, pricing)
    tokens = (
        usage.input_tokens * rates["input"]
        + usage.output_tokens * rates["output"]
        + usage.cache_write_tokens * rates["cache_write_1h"]
        + usage.cache_read_tokens * rates["cache_read"]
    ) / PER_MILLION
    if batch:
        tokens *= float(pricing["batch_discount"])
    searches = usage.web_searches * float(pricing["web_search_per_1000"]) / 1000  # not discounted in Batch
    return round(tokens + searches, 6)


def web_search_price(pricing: dict[str, Any] | None = None) -> float:
    pricing = pricing or load_pricing()
    return float(pricing["web_search_per_1000"]) / 1000


def monthly_budget(name: str, pricing: dict[str, Any] | None = None) -> float:
    pricing = pricing or load_pricing()
    return float(pricing["budgets_usd_per_month"][name])
