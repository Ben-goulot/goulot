"""Official Chinese market prices: the ten-day releases of the National Bureau of Statistics (rank 2, docs/SPEC.md,
section 6).

About the 4th, 14th and 24th of each month, the NBS publishes « 流通领域重要生产资料市场价格变动情况 », a table of
50 products in 9 families. The list of releases (`/sj/zxfb/`, then `index_1.html`…) is read for links whose text is
the title of that release; each release table is read as text, and only the products of the allowlist below are kept
(exact name before the specification in brackets; the coal blends dropped from the list in the 2020s are left
out). Every product becomes an instrument, so that any material, input or derivative can be linked to it; release
URLs already read are kept in `nbs_releases` and never fetched again.
"""

from __future__ import annotations

import calendar
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx2

from goulot.collect.base import fold
from goulot.db.connection import Database, utcnow
from goulot.net.client import fetch
from goulot.prices import store as prices

log = logging.getLogger("goulot.prices.nbs")

SOURCE = "nbs_china_prices"
BASE = "https://www.stats.gov.cn/sj/zxfb/"
TITLE = "流通领域重要生产资料市场价格变动情况"
TITLE_RE = re.compile(r"(\d{4})年(\d{1,2})月(上|中|下)旬" + TITLE)
MAX_LIST_PAGES = 80  # about 15 releases of all kinds per page: 67 pages reached back to 2021 in October 2026
MAX_RELEASES_PER_RUN = 250
MAX_PAGE_BYTES = 2 * 1024 * 1024
HISTORY_START = date(2016, 1, 1)


@dataclass(frozen=True)
class Product:
    slug: str
    zh: str  # name before the specification, as printed in the table
    fr: str
    cas: str | None
    unit: str


PRODUCTS = (
    Product("rebar", "螺纹钢", "Rond à béton", None, "t"),
    Product("wire_rod", "线材", "Fil machine", None, "t"),
    Product("medium_plate", "普通中板", "Tôle moyenne", None, "t"),
    Product("hot_rolled_coil", "热轧普通板卷", "Bobine laminée à chaud", None, "t"),
    Product("seamless_pipe", "无缝钢管", "Tube sans soudure", None, "t"),
    Product("angle_steel", "角钢", "Cornière", None, "t"),
    Product("copper", "电解铜", "Cuivre électrolytique", "7440-50-8", "t"),
    Product("aluminium", "铝锭", "Aluminium en lingots", "7429-90-5", "t"),
    Product("lead", "铅锭", "Plomb en lingots", "7439-92-1", "t"),
    Product("zinc", "锌锭", "Zinc en lingots", "7440-66-6", "t"),
    Product("sulfuric_acid", "硫酸", "Acide sulfurique 98 %", "7664-93-9", "t"),
    Product("caustic_soda", "烧碱", "Soude liquide 32 %", "1310-73-2", "t"),
    Product("methanol", "甲醇", "Méthanol", "67-56-1", "t"),
    Product("benzene", "纯苯", "Benzène", "71-43-2", "t"),
    Product("ethanol", "乙醇", "Éthanol 95 %", "64-17-5", "t"),
    Product("lldpe", "聚乙烯", "Polyéthylène LLDPE", "9002-88-4", "t"),
    Product("polypropylene", "聚丙烯", "Polypropylène", "9003-07-0", "t"),
    Product("acetic_acid", "冰醋酸", "Acide acétique", "64-19-7", "t"),
    Product("butadiene_rubber", "顺丁胶", "Caoutchouc butadiène", "9003-17-2", "t"),
    Product("polyester_yarn", "涤纶长丝", "Fil de polyester", None, "t"),
    Product("lfp", "磷酸铁锂", "Phosphate de fer lithié", "15365-14-7", "t"),
    Product("lng", "液化天然气", "GNL", None, "t"),
    Product("lpg", "液化石油气", "GPL", None, "t"),
    Product("gasoline", "汽油", "Essence", None, "t"),
    Product("diesel", "柴油", "Gazole", None, "t"),
    Product("paraffin", "石蜡", "Paraffine", "8002-74-2", "t"),
    Product("anthracite", "无烟煤", "Anthracite", None, "t"),
    Product("thermal_coal", "山西优混", "Charbon vapeur (Shanxi)", None, "t"),
    Product("coking_coal", "焦煤", "Charbon à coke", None, "t"),
    Product("coke", "焦炭", "Coke", None, "t"),
    Product("cement", "普通硅酸盐水泥", "Ciment Portland", None, "t"),
    Product("float_glass", "浮法平板玻璃", "Verre plat", None, "t"),
    Product("polysilicon", "多晶硅", "Polysilicium", "7440-21-3", "kg"),
    Product("rice", "稻米", "Riz", None, "t"),
    Product("wheat", "小麦", "Blé", None, "t"),
    Product("corn", "玉米", "Maïs", None, "t"),
    Product("cotton", "棉花", "Coton", None, "t"),
    Product("hogs", "生猪", "Porc vivant", None, "kg"),
    Product("soybean", "大豆", "Soja", None, "t"),
    Product("soybean_meal", "豆粕", "Tourteau de soja", None, "t"),
    Product("peanut", "花生", "Arachide", None, "t"),
    Product("sugar", "白糖", "Sucre blanc", None, "t"),
    Product("urea", "尿素", "Urée", "57-13-6", "t"),
    Product("map", "磷肥", "Phosphate monoammonique", "7722-76-1", "t"),
    Product("potash", "钾肥", "Chlorure de potassium", "7447-40-7", "t"),
    Product("compound_fertilizer", "复合肥", "Engrais composé", None, "t"),
    Product("glyphosate", "农药", "Glyphosate 95 %", "1071-83-6", "t"),
    # Published until the products were revised (2021 releases): kept for the history.
    Product("hot_rolled_sheet", "热轧普通薄板", "Tôle mince laminée à chaud", None, "t"),
    Product("styrene", "苯乙烯", "Styrène", "100-42-5", "t"),
    Product("pvc", "聚氯乙烯", "PVC", "9002-86-2", "t"),
    Product("natural_rubber", "天然橡胶", "Caoutchouc naturel", None, "t"),
    Product("pulp", "纸浆", "Pâte à papier", None, "t"),
    Product("corrugated_paper", "瓦楞纸", "Papier cannelure", None, "t"),
)
BY_ZH = {p.zh: p for p in PRODUCTS}
BY_CAS = {p.cas: p for p in PRODUCTS if p.cas}
# Names of chain nodes (French product names or node ids, folded) that are the same product: an input or a
# derivative of a material gets the official price proposed, to be validated by the user.
ALIASES = {
    "sulfuric_acid": ("acide sulfurique", "sulfuric acid"), "caustic_soda": ("soude caustique", "hydroxyde de sodium",
                                                                           "caustic soda", "sodium hydroxide"),
    "methanol": ("methanol",), "benzene": ("benzene",), "ethanol": ("ethanol",), "lldpe": ("polyethylene", "pehd",
                                                                                         "lldpe"),
    "polypropylene": ("polypropylene",), "acetic_acid": ("acide acetique", "acetic acid"), "urea": ("uree", "urea"),
    "styrene": ("styrene",), "pvc": ("pvc", "polychlorure de vinyle"), "lng": ("gnl", "lng"), "lpg": ("gpl", "lpg"),
    "copper": ("cuivre", "copper"), "aluminium": ("aluminium",), "zinc": ("zinc",), "lead": ("plomb", "lead"),
    "polysilicon": ("polysilicium", "polysilicon"), "potash": ("chlorure de potassium", "potasse", "potash"),
    "lfp": ("phosphate de fer lithie", "lfp"), "natural_rubber": ("caoutchouc naturel",),
    "butadiene_rubber": ("caoutchouc butadiene", "polybutadiene"), "paraffin": ("paraffine", "paraffin"),
}
_BY_SLUG = {p.slug: p for p in PRODUCTS}
_BY_NAME = {**_BY_SLUG, **{alias: _BY_SLUG[slug] for slug, names in ALIASES.items() for alias in names}}


def match_node(node_key: str, product: str) -> Product | None:
    """The NBS product a chain node stands for, by its id or its exact product name (folded), or None."""
    return _BY_NAME.get(node_key) or _BY_NAME.get(" ".join(fold(product).replace("_", " ").split()))


def instrument_id(product: Product) -> str:
    return f"{SOURCE}:{product.slug}"


def period_of(title: str) -> date | None:
    """Last day covered by a release: the 10th (上旬), the 20th (中旬) or the end of the month (下旬)."""
    m = TITLE_RE.search("".join(title.split()))
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    day = {"上": 10, "中": 20}.get(m.group(3)) or calendar.monthrange(year, month)[1]
    return date(year, month, day)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.href, self.text = dict(attrs).get("href"), []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.href:
            self.links.append((self.href, "".join("".join(self.text).split())))
            self.href = None

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.text.append(data)


def parse_list(html: str, page_url: str) -> list[tuple[str, date]]:
    """(absolute URL, period) of the price releases listed on a page; other links and other hosts are ignored."""
    parser = _Links()
    parser.feed(html)
    out: dict[str, date] = {}
    for href, text in parser.links:
        period = period_of(text)
        if period is None:
            continue
        url = urljoin(page_url, href)
        parts = urlsplit(url)
        if parts.scheme == "https" and parts.hostname == "www.stats.gov.cn" and parts.path.startswith("/sj/zxfb/"):
            out.setdefault(url, period)
    return sorted(out.items(), key=lambda item: item[1])


class _Cells(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.cell: list[str] | None = None
        self.title: list[str] | None = None
        self.title_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.rows.append([])
        elif tag == "td" and self.rows:
            self.cell = []
        elif tag == "title":
            self.title = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self.cell is not None:
            self.rows[-1].append("".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "title" and self.title is not None:
            self.title_text = "".join(self.title)
            self.title = None

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)
        if self.title is not None:
            self.title.append(data)


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 and value == value else None


def parse_release(html: str) -> tuple[date | None, dict[str, float]]:
    """(period from the page title, {slug: price in yuan}) from the first price table of a release."""
    parser = _Cells()
    parser.feed(html)
    found: dict[str, float] = {}
    for row in parser.rows:
        if len(row) != 5:
            continue
        name = re.split(r"[（(]", row[0], maxsplit=1)[0]
        product = BY_ZH.get(name)
        price = _number(row[2])
        if product is not None and price is not None:
            found.setdefault(product.slug, price)  # the table is printed twice on the page
    return period_of(parser.title_text), found


def ensure_instruments(conn: sqlite3.Connection) -> None:
    for p in PRODUCTS:
        prices.ensure_instrument(conn, SOURCE, p.slug, f"{p.fr} (prix officiel chinois)", rank=2, frequency="tenday",
                                 unit=f"CNY/{p.unit}", currency="CNY", params={"zh": p.zh})


def _get(client: httpx2.Client, url: str) -> str | None:
    result = fetch(client, url, max_bytes=MAX_PAGE_BYTES)
    if result.status == 404:
        return None
    if result.status != 200:
        raise httpx2.HTTPError(f"HTTP {result.status}")
    return result.content.decode("utf-8", errors="replace")


def collect(db: Database, client: httpx2.Client, *, start: date = HISTORY_START,
            max_pages: int = MAX_LIST_PAGES) -> int:
    """Read the releases not read yet, newest list pages first, back to `start`. Returns prices written.

    The walk stops at the first list page whose releases are all known, or that reaches back before `start`, so
    a daily run costs one list page."""
    with db.transaction() as conn:
        ensure_instruments(conn)
        known = {r[0] for r in conn.execute("SELECT url FROM nbs_releases")}
    todo: list[tuple[str, date]] = []
    for page in range(max_pages):
        url = BASE if page == 0 else f"{BASE}index_{page}.html"
        html = _get(client, url)
        if html is None:
            break
        listed = parse_list(html, url)
        fresh = [(u, p) for u, p in listed if u not in known and p >= start]
        todo += fresh
        if (listed and not fresh) or any(p < start for _, p in listed) or len(todo) >= MAX_RELEASES_PER_RUN:
            break
    written = 0
    for url, listed_period in sorted(todo, key=lambda item: item[1])[:MAX_RELEASES_PER_RUN]:
        html = _get(client, url)
        if html is None:
            continue
        period, found = parse_release(html)
        period = period or listed_period
        with db.transaction() as conn:
            for slug, value in found.items():
                written += prices.upsert(conn, f"{SOURCE}:{slug}", [(period, value)])
            conn.execute("INSERT OR IGNORE INTO nbs_releases (url, period, products, fetched_at) VALUES (?, ?, ?, ?)",
                         (url, period.isoformat(), len(found), utcnow()))
    if todo:
        log.info("nbs releases read: %s, prices written: %s", len(todo), written)
    return written
