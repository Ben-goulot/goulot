"""Prompt construction from versioned files and whitelisted fields (docs/SECURITY.md, AI).

External content goes inside explicit tags with `<` and `>` escaped, so that no text can close a tag.
Builders take only the fields listed here: purchase data, Ma chaîne and verdict inputs never reach a prompt.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from goulot.settings import REPO_ROOT

PROMPTS_DIR = REPO_ROOT / "prompts"
SCHEMAS_DIR = REPO_ROOT / "schemas"

# Material fields allowed in the reconstruction prompts. Nothing else is read from the material record.
MATERIAL_FIELDS = ("name", "cas", "formula", "purchased_form")


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@lru_cache(maxsize=32)
def load_prompt(name: str, directory: Path = PROMPTS_DIR) -> str:
    return (directory / f"{name}.md").read_text(encoding="utf-8").strip()


@lru_cache(maxsize=32)
def load_schema_text(name: str, directory: Path = SCHEMAS_DIR) -> str:
    return (directory / f"{name}.json").read_text(encoding="utf-8")


def tag(name: str, body: str) -> str:
    return f"<{name}>\n{body}\n</{name}>"


def material_block(material: Mapping[str, Any]) -> str:
    lines = []
    for field in MATERIAL_FIELDS:
        value = str(material.get(field) or "unknown").strip() or "unknown"
        lines.append(f"{field}: {escape(value)}")
    return tag("material", "\n".join(lines))


def sources_block(sources: Iterable[Mapping[str, str]]) -> str:
    """Source list for the structuring call: identifier, title, publisher, date. Never a URL."""
    lines = [
        f"{escape(s['ref'])} | {escape(s.get('title') or 'unknown')} | {escape(s.get('publisher') or 'unknown')}"
        f" | {escape(s.get('page_age') or 'unknown')}"
        for s in sources
    ]
    return tag("sources", "\n".join(lines) or "(aucune)")
