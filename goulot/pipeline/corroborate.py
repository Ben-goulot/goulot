"""Corroboration (docs/SPEC.md, section 4, step 5; `config/reliability.yaml`).

Reliability by domain: official 0.9, agency or specialized press 0.7, general press 0.5, unknown 0.3. A
syndication site takes the reliability of the publisher given by the platform's structured data (Google News
`<source url>`), never from a byline or `cited_origin`; a paid press release counts 0.3 wherever it appears.
A lineage groups an original source and its copies; it takes the reliability of its best source.
`c_fait = 1 − Π(1 − r)` over lineages. Confirmed if `c_fait ≥ 0.75` and one lineage reaches 0.7.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from goulot.collect.base import canonical_url, fold
from goulot.settings import CONFIG_DIR


@lru_cache(maxsize=2)
def config(path: Path = CONFIG_DIR / "reliability.yaml") -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def _under(domain: str, parent: str) -> bool:
    return domain == parent or domain.endswith("." + parent)


def domain_reliability(domain: str, path: str = "", extra: dict[str, float] | None = None) -> float:
    cfg = config()
    scores = cfg["classes"]
    domain = domain.lower().removeprefix("www.")
    if extra:
        for d, r in extra.items():
            if _under(domain, d):
                return r
    if any(_under(domain, d) for d in cfg["press_release_wires"]) or any(
        path.lower().startswith(p) for p in cfg["press_release_paths"]
    ):
        return float(scores["unknown"])
    if any(domain.endswith(s) for s in cfg["official"]["suffixes"]) or any(
        _under(domain, d) for d in cfg["official"]["domains"]
    ):
        return float(scores["official"])
    for cls in ("agency_specialized", "general_press"):
        if any(_under(domain, d) for d in cfg[cls]["domains"]):
            return float(scores[cls])
    return float(scores["unknown"])


def article_reliability(domain: str, provider_domain: str, url_path: str = "",
                        extra: dict[str, float] | None = None) -> float:
    syndication = config()["syndication_hosts"]
    if any(_under(domain, d) for d in syndication):
        # Only the platform's structured data names the real publisher; without it, unknown.
        return domain_reliability(provider_domain, "", extra) if provider_domain else float(
            config()["classes"]["unknown"])
    return domain_reliability(domain, url_path, extra)


def press_group(domain: str) -> str:
    for group in config().get("groups", []):
        if any(_under(domain, d) for d in group):
            return "group:" + str(group[0])
    return ""


def shingles(text: str, n: int = 5) -> set[str]:
    words = re.findall(r"[a-z0-9]+", fold(text))
    return {" ".join(words[i:i + n]) for i in range(max(len(words) - n + 1, 1))} if words else set()


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def same_title(a: str, b: str) -> bool:
    """The same headline, long enough not to be a coincidence: one article read twice (by Google News and by the
    publisher's own feed) or a wire story reprinted as is."""
    words = re.findall(r"[a-z0-9]+", fold(a))
    minimum = int(config()["lineage"].get("same_title", {}).get("min_words", 6))
    return len(words) >= minimum and words == re.findall(r"[a-z0-9]+", fold(b))


def same_story(title_a: str, grams_a: set[str], title_b: str, grams_b: set[str]) -> bool:
    """Near-identical text (5-word shingles of title and excerpt) or the same headline."""
    threshold = float(config()["lineage"]["text_similarity"]["threshold"])
    return jaccard(grams_a, grams_b) >= threshold or same_title(title_a, title_b)


@dataclass
class ArticleRef:
    article_id: int
    url: str
    domain: str
    provider_domain: str
    title: str
    excerpt: str
    cited_origin: str
    reliability: float


def lineages(articles: Sequence[ArticleRef]) -> dict[int, str]:
    """article_id -> lineage key. Same canonical URL, near-identical text (5-word shingles, Jaccard ≥ 0.6) or the
    same headline, same cited origin or same press group put two articles in the same lineage (union-find)."""
    parent = {a.article_id: a.article_id for a in articles}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    grams = {a.article_id: shingles(f"{a.title} {a.excerpt}") for a in articles}
    for i, a in enumerate(articles):
        for b in articles[i + 1:]:
            same = (
                canonical_url(a.url) == canonical_url(b.url)
                or same_story(a.title, grams[a.article_id], b.title, grams[b.article_id])
                or (a.cited_origin not in ("", "unknown") and fold(a.cited_origin) == fold(b.cited_origin))
                or (press_group(a.domain) and press_group(a.domain) == press_group(b.domain))
            )
            if same:
                union(a.article_id, b.article_id)
    return {a.article_id: hashlib.sha256(str(find(a.article_id)).encode()).hexdigest()[:16] for a in articles}


@dataclass
class Corroboration:
    c_fait: float
    lineages: int
    best: float
    confirmed: bool


def corroborate(articles: Sequence[ArticleRef]) -> tuple[Corroboration, dict[int, str]]:
    keys = lineages(articles)
    best_by_lineage: dict[str, float] = {}
    for a in articles:
        best_by_lineage[keys[a.article_id]] = max(best_by_lineage.get(keys[a.article_id], 0.0), a.reliability)
    product = 1.0
    for r in best_by_lineage.values():
        product *= 1 - r
    c_fait = round(1 - product, 4)
    best = max(best_by_lineage.values(), default=0.0)
    cfg = config()["confirmation"]
    confirmed = c_fait >= float(cfg["c_fait_min"]) and best >= float(cfg["min_lineage_reliability"])
    return Corroboration(c_fait, len(best_by_lineage), best, confirmed), keys
