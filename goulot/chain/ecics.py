"""Customs codes from ECICS, the European customs inventory of chemical substances (docs/SPEC.md, section 3).

ECICS has no API and its full download is a ZIP archive, which the app never unpacks (docs/SECURITY.md, threat 11).
The public result list of the consultation page is read instead: one small HTML table per CAS number, whose cells
are parsed as text only. A CN code (8 digits) gives the HS code (its first 6 digits). ECICS is a complement:
when it does not answer, the codes proposed by the reconstruction stay « à vérifier ».
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx2

from goulot.net.client import fetch

log = logging.getLogger("goulot.chain.ecics")

LIST_URL = "https://ec.europa.eu/taxation_customs/dds2/ecics/chemicalsubstance_list.jsp"
PAGE_URL = "https://ec.europa.eu/taxation_customs/dds2/ecics/"
MAX_BYTES = 512 * 1024
MAX_ROWS = 25  # one result page
CN_RE = re.compile(r"^\d{8}$")


@dataclass(frozen=True)
class CnCode:
    cn8: str
    cus: str
    name: str

    @property
    def hs6(self) -> str:
        return self.cn8[:6]


class _Table(HTMLParser):
    """Text of the cells of the result table (`tblData`), row by row; everything else is ignored."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.rows: list[list[str]] = []
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table" and dict(attrs).get("id") == "tblData":
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.rows.append([])
        elif self.in_table and tag == "td" and self.rows:
            self.cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self.in_table = False
        elif tag == "td" and self.cell is not None:
            self.rows[-1].append(" ".join("".join(self.cell).split()))
            self.cell = None

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)


def parse(html: str, cas: str) -> list[CnCode]:
    """CN codes of the rows whose CAS cell is exactly `cas` (columns: CUS, CN code, CAS RN, EC, UN, nomen., name)."""
    table = _Table()
    table.feed(html)
    out: list[CnCode] = []
    for row in table.rows[:MAX_ROWS + 1]:
        if len(row) < 7 or row[2] != cas or not CN_RE.match(row[1]):
            continue
        if all(c.cn8 != row[1] for c in out):
            out.append(CnCode(cn8=row[1], cus=row[0][:20], name=row[6][:120]))
    return out


def lookup(client: httpx2.Client, cas: str) -> list[CnCode]:
    """CN codes for a validated CAS number; [] when ECICS is unavailable or does not know the substance."""
    try:
        result = fetch(client, LIST_URL, params={"Lang": "en", "offset": "0", "Cas": cas, "sortOrder": "1"},
                       max_bytes=MAX_BYTES)
    except (httpx2.HTTPError, ValueError) as exc:
        log.info("ecics unavailable: %s", type(exc).__name__)
        return []
    if result.status != 200:
        log.info("ecics unavailable: HTTP %s", result.status)
        return []
    return parse(result.content.decode("utf-8", errors="replace"), cas)


def page_url(cas: str) -> str:
    """Consultation page shown as the source (built by the code, never taken from a response)."""
    return f"{PAGE_URL}?Lang=en&Cas={cas}"
