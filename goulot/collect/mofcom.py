"""China's Ministry of Commerce (MOFCOM): its policy announcements (政策发布), read at the source (22 November 2026).

Export controls (gallium and germanium in 2023, the dual-use items to the United States in 2024), anti-dumping
investigations and their suspensions are published there first, in Chinese. The list page is built by a script; the
home page lists the latest announcements as plain links, so the home page is read. Each announcement's own page is
read too, as text and never executed: the products it names and their customs codes are often in the body only
(an annex of codes, a list of items), not in the title.

External content, untrusted: links are kept only on the ministry's own host and announcement path, the page is
reduced to text, bounded, and the material names and customs codes found in it are recorded as details, for the rule
filter. Nothing from the page is followed further.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import httpx2

from goulot.collect.base import Item, clean_text, fold
from goulot.collect.sources import SourceError
from goulot.net.client import fetch

HOME = "https://www.mofcom.gov.cn/"
ANNOUNCEMENT = re.compile(r"^(?:https://www\.mofcom\.gov\.cn)?(/zwgk/zcfb/art/(20\d\d)/art_[0-9a-f]{32}\.html)$")
MAX_ANNOUNCEMENTS = 12  # the home page lists about eight
PAGE_MAX_BYTES = 2 * 1024 * 1024
BODY_CHARS = 60_000
EXCERPT_CHARS = 1_500
MAX_CODES = 40
OLDEST = timedelta(days=45)  # an announcement still on the home page after that is no news
# Customs codes as Chinese texts write them: 8 or 10 digits (8112923010), or « 8112.92 » with its sub-headings.
CODE = re.compile(r"(?<![\d.])(\d{4}(?:\.\d{2}(?:\.\d{2,4})?|\d{4}(?:\d{2})?))(?![\d.])")
DATE = re.compile(r"(20\d\d)\s*(?:-|年|/|\.)\s*(\d{1,2})\s*(?:-|月|/|\.)\s*(\d{1,2})")
# Blocks that are not the announcement: the ministry's sidebars, menus and footers.
NOISE = re.compile(r"<(script|style|aside|nav|header|footer|noscript)\b.*?</\1\s*>", re.S | re.I)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str]] = []
        self._href = ""
        self._title = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            values = dict(attrs)
            self._href, self._title, self._text = values.get("href") or "", values.get("title") or "", []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.found.append((self._href, self._title or "".join(self._text)))
            self._href = ""


def announcement_links(page: str) -> list[tuple[str, str]]:
    """(absolute URL, title) of the policy announcements a page links to, newest first as listed, once each."""
    parser = _Links()
    parser.feed(page)
    out: dict[str, str] = {}
    for href, title in parser.found:
        m = ANNOUNCEMENT.match(href.strip())
        if m and m.group(1) not in out:
            out[m.group(1)] = clean_text(title, 300)
    return [("https://www.mofcom.gov.cn" + path, title) for path, title in out.items()][:MAX_ANNOUNCEMENTS]


def page_text(page: str) -> str:
    return clean_text(NOISE.sub(" ", page), BODY_CHARS)


def published(text: str, year: str) -> datetime | None:
    """The first date of the announcement's own year in its text: the page shows its publication date first."""
    for m in DATE.finditer(text):
        y, mo, d = (int(g) for g in m.groups())
        if str(y) == year and 1 <= mo <= 12 and 1 <= d <= 31:
            try:
                return datetime(y, mo, d, tzinfo=UTC)
            except ValueError:
                continue
    return None


def customs_codes(text: str) -> list[str]:
    codes = dict.fromkeys(c.replace(".", "") for c in CODE.findall(text) if not c.startswith(("19", "20")))
    return list(codes)[:MAX_CODES]


def _get(client: httpx2.Client, url: str) -> str:
    result = fetch(client, url, max_bytes=PAGE_MAX_BYTES)
    if result.status != 200:
        raise SourceError(f"HTTP {result.status}")
    return result.content.decode("utf-8", errors="replace")


def announcements(client: httpx2.Client, names: list[str], now: datetime) -> list[Item]:
    """The announcements of the home page, each with its text (an announcement already stored is recognised by its
    address and not kept twice). `names`: the names of the watched materials in every
    language the chains know (Chinese included); those found in the text are recorded, for the rule filter."""
    items = []
    for url, title in announcement_links(_get(client, HOME)):
        year = url.split("/art/")[1][:4]
        try:
            text = page_text(_get(client, url))
        except (SourceError, httpx2.HTTPError):
            text = ""  # the title alone: an official title, still worth the filter
        when = published(text, year)
        if when is not None and when < now - OLDEST:
            continue
        folded = fold(text)
        found = [n for n in names if n and fold(n) in folded]
        body = text.replace(title, "", 1).strip() if title else text
        items.append(Item(
            source="mofcom", kind="policy", url=url, title=title or body[:120], published_at=when,
            excerpt=body[:EXCERPT_CHARS], lang="zh", domain="mofcom.gov.cn", key=f"mofcom:{url.rsplit('/', 1)[1]}",
            details={"country": "CN", "names": found[:20], "hs_codes": customs_codes(text)}, queried=False,
        ))
    return items
