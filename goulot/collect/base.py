"""Shared pieces of the collectors (docs/SPEC.md, section 4).

External content is data: HTML is reduced to text by `nh3`, lengths are capped, URLs are kept only in https
(or http for display as text, never as a link) and canonicalized for deduplication.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import nh3
import yaml

from goulot.settings import CONFIG_DIR

MAX_TITLE = 300
MAX_EXCERPT = 1000
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ocid$|cmpid$)")


@dataclass
class Item:
    """One collected item, before the rule filter."""

    source: str
    kind: str  # news | hazard | logistics | policy
    url: str
    title: str
    published_at: datetime | None = None
    lang: str = ""
    excerpt: str = ""
    domain: str = ""
    provider_domain: str = ""
    lat: float | None = None
    lon: float | None = None
    magnitude: float | None = None
    key: str = ""  # stable identity for feeds without URL (e.g. a river reading); defaults to the URL
    details: dict[str, Any] = field(default_factory=dict)

    def identity(self) -> str:
        return hashlib.sha256((self.key or canonical_url(self.url)).encode()).hexdigest()


def clean_text(raw: str | None, limit: int = MAX_EXCERPT) -> str:
    """HTML to plain text: every tag removed by nh3, entities decoded, spaces collapsed, length capped."""
    if not raw:
        return ""
    text = nh3.clean(raw, tags=set(), attributes={}, strip_comments=True)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if not TRACKING_PARAMS.match(k)]
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def fold(text: str) -> str:
    """Lowercase without accents, for matching rules across languages and spellings."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def parse_datetime(value: Any) -> datetime | None:
    """ISO 8601, RFC 822 (RSS), GDELT compact (20251016T120000Z) or epoch milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
    text = str(value).strip()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T?(\d{2})(\d{2})(\d{2})Z?$", text)
    if m:
        y, mo, d, h, mi, sec = (int(g) for g in m.groups())
        return datetime(y, mo, d, h, mi, sec, tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat(timespec="seconds") if dt else None


@lru_cache(maxsize=4)
def load_sources(path: Path = CONFIG_DIR / "sources.yaml") -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {str(s["id"]): s for s in data.get("sources", [])}


def enabled(source_id: str) -> bool:
    return bool(load_sources().get(source_id, {}).get("enabled", False))
