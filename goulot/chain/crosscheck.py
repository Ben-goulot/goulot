"""Step 3: minimal cross-check of the country shares (docs/SPEC.md, section 3).

- UN Comtrade: export shares of the purchased material's HS code, latest complete year, compared with the
  tier-1 country nodes.
- USGS Mineral Commodity Summaries (world production table of the data release, CSV), compared with the
  country nodes of the same commodity. Used only when `data_url` of `usgs_mcs` is set in `config/sources.yaml`.
A relative gap above 30 % drops the node to low confidence with a comment. Missing data changes nothing.

Like is compared with like (2026-11-11): a reference has a basis (exports for Comtrade, production for USGS) and a
node's share is compared only when it has the same basis, or none (chains structured before shares had one). An
export share next to a production share is not a gap: the reference is then noted on the node, without any flag.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx2

from goulot.chain.countries import ISO3_TO_ISO2
from goulot.net.client import fetch

log = logging.getLogger("goulot.chain.crosscheck")

MAX_RELATIVE_GAP = 0.30
COMTRADE_PUBLIC = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
COMTRADE_KEYED = "https://comtradeapi.un.org/data/v1/get/C/A/HS"
HS6 = re.compile(r"^\d{6}$")

# USGS writes country names in English. Producers commonly listed in the world production tables.
USGS_NAMES: dict[str, str] = {
    "argentina": "AR", "australia": "AU", "austria": "AT", "belgium": "BE", "bolivia": "BO", "brazil": "BR",
    "burma": "MM", "canada": "CA", "chile": "CL", "china": "CN", "colombia": "CO", "congo (kinshasa)": "CD",
    "cuba": "CU", "egypt": "EG", "finland": "FI", "france": "FR", "germany": "DE", "ghana": "GH", "greece": "GR",
    "guinea": "GN", "hungary": "HU", "india": "IN", "indonesia": "ID", "iran": "IR", "ireland": "IE", "israel": "IL",
    "italy": "IT", "jamaica": "JM", "japan": "JP", "kazakhstan": "KZ", "korea, republic of": "KR", "laos": "LA",
    "madagascar": "MG", "malaysia": "MY", "mali": "ML", "mexico": "MX", "mongolia": "MN", "morocco": "MA",
    "mozambique": "MZ", "namibia": "NA", "netherlands": "NL", "new caledonia": "NC", "nigeria": "NG", "norway": "NO",
    "oman": "OM", "pakistan": "PK", "peru": "PE", "philippines": "PH", "poland": "PL", "portugal": "PT",
    "qatar": "QA", "russia": "RU", "rwanda": "RW", "saudi arabia": "SA", "serbia": "RS", "slovakia": "SK",
    "south africa": "ZA", "spain": "ES", "sweden": "SE", "switzerland": "CH", "taiwan": "TW", "tanzania": "TZ",
    "thailand": "TH", "turkey": "TR", "türkiye": "TR", "ukraine": "UA", "united arab emirates": "AE",
    "united kingdom": "GB", "united states": "US", "uzbekistan": "UZ", "venezuela": "VE", "vietnam": "VN",
    "zambia": "ZM", "zimbabwe": "ZW",
    # Added from the MCS 2026 world tables (12 November 2026).
    "afghanistan": "AF", "algeria": "DZ", "angola": "AO", "armenia": "AM", "azerbaijan": "AZ", "bahrain": "BH",
    "belarus": "BY", "bhutan": "BT", "botswana": "BW", "bulgaria": "BG", "burundi": "BI", "cameroon": "CM",
    "cyprus": "CY", "czechia": "CZ", "côte d’ivoire": "CI", "côte d'ivoire": "CI", "denmark": "DK", "ecuador": "EC",
    "estonia": "EE", "ethiopia": "ET", "gabon": "GA", "georgia": "GE", "greenland": "GL", "guatemala": "GT",
    "iceland": "IS", "jordan": "JO", "kenya": "KE", "korea, north": "KP", "kuwait": "KW", "kyrgyzstan": "KG",
    "latvia": "LV", "lesotho": "LS", "lithuania": "LT", "mauritania": "MR", "new zealand": "NZ",
    "papua new guinea": "PG", "senegal": "SN", "sierra leone": "SL", "sri lanka": "LK", "syria": "SY",
    "tajikistan": "TJ", "togo": "TG", "trinidad and tobago": "TT", "tunisia": "TN", "turkmenistan": "TM",
    "uganda": "UG",
}


USGS_MAX_BYTES = 16 * 1024 * 1024
# French product words of a chain → USGS commodity names (lower case). Only commodities the MCS world tables cover.
USGS_COMMODITIES: dict[str, str] = {
    "lithium": "lithium", "spodumène": "lithium", "spodumene": "lithium", "gallium": "gallium",
    "germanium": "germanium", "bauxite": "bauxite", "alumine": "alumina", "alumina": "alumina",
    "aluminium": "aluminum", "aluminum": "aluminum", "zinc": "zinc", "cuivre": "copper", "copper": "copper",
    "nickel": "nickel", "cobalt": "cobalt", "graphite": "graphite", "antimoine": "antimony", "antimony": "antimony",
    "tungstène": "tungsten", "tungsten": "tungsten", "molybdène": "molybdenum", "molybdenum": "molybdenum",
    "bismuth": "bismuth", "indium": "indium", "étain": "tin", "manganèse": "manganese", "manganese": "manganese",
    "chrome": "chromium", "chromium": "chromium", "vanadium": "vanadium",
    "magnésium": "magnesium metal", "magnesium": "magnesium metal", "silicium": "silicon", "silicon": "silicon",
    "ammoniac": "nitrogen (fixed)—ammonia", "ammonia": "nitrogen (fixed)—ammonia", "ilménite": "titanium mineral "
    "concentrates", "rutile": "titanium mineral concentrates", "plomb": "lead", "lead": "lead", "argent": "silver",
    "platine": "platinum", "palladium": "palladium", "terres": "rare earths", "potasse": "potash", "potash": "potash",
    "phosphate": "phosphate rock", "soude": "soda ash", "fluorine": "fluorspar", "fluorite": "fluorspar",
    "fluorspar": "fluorspar", "baryte": "barite", "barite": "barite", "bore": "boron", "borax": "boron",
    "iode": "iodine", "iodine": "iodine", "soufre": "sulfur", "sulfur": "sulfur", "sel": "salt", "salt": "salt",
    "fer": "iron ore", "niobium": "niobium", "tantale": "tantalum", "tantalum": "tantalum", "rhénium": "rhenium",
    "sélénium": "selenium", "tellure": "tellurium", "cadmium": "cadmium", "zirconium": "zirconium",
    "hélium": "helium", "helium": "helium", "ciment": "cement", "gypse": "gypsum",
}
WORD = re.compile(r"[a-zà-ÿ]+")
# Which production series a node's share is about, when a commodity has two (copper: mine and refinery).
REFINERY_WORDS = ("raffin", "affin", "refin", "fonderie", "smelter", "métal", "metal", "cathode", "électrolyt")
MINE_WORDS = ("mine", "minerai", "extraction", "concentré", "concentrate", "ore", "gisement")
NUMBER = re.compile(r"^\d[\d,]*(\.\d+)?$")


def usgs_hint(text: str) -> str | None:
    """« mine » or « refinery » from a node's product and name, or None when it says neither or both."""
    low = (text or "").lower()
    refinery = any(w in low for w in REFINERY_WORDS)
    mine = any(re.search(rf"\b{w}", low) for w in MINE_WORDS)
    return "refinery" if refinery and not mine else "mine" if mine and not refinery else None


def usgs_commodity(product: str) -> str | None:
    """USGS commodity of a node's product, from its words (« Concentré de spodumène » → lithium)."""
    for word in WORD.findall((product or "").lower()):
        if word in USGS_COMMODITIES:
            return USGS_COMMODITIES[word]
    return None


def fetch_usgs_csv(client: httpx2.Client, url: str) -> str | None:
    """The MCS world production table, through the app's client (allowlist, HTTPS, size cap). None if unavailable."""
    if not url:
        return None
    result = fetch(client, url, max_bytes=USGS_MAX_BYTES)
    if result.status != 200:
        log.info("usgs table unavailable: status=%s", result.status)
        return None
    try:
        return result.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return result.content.decode("cp1252", errors="replace")  # the MCS 2026 table is in Windows-1252


@dataclass
class Reference:
    source: str  # "UN Comtrade 2024 (exportations)" or "USGS MCS (production)"
    url: str
    basis: str = "unknown"  # exports | production: what the shares are shares of
    shares: dict[str, float] = field(default_factory=dict)  # ISO2 -> % of the world


def comtrade_export_shares(
    client: httpx2.Client, hs6: str, year: int, api_key: str | None = None,
) -> Reference | None:
    if not HS6.match(hs6):
        return None
    params = {"cmdCode": hs6, "flowCode": "X", "period": str(year), "partnerCode": "0", "includeDesc": "true"}
    url = COMTRADE_KEYED if api_key else COMTRADE_PUBLIC
    headers = {"Ocp-Apim-Subscription-Key": api_key} if api_key else None  # header, never in the URL
    result = fetch(client, url, params=params, headers=headers, max_bytes=4 * 1024 * 1024)
    if result.status != 200:
        log.info("comtrade unavailable: status=%s", result.status)
        return None
    try:
        rows = json.loads(result.content).get("data") or []
    except (json.JSONDecodeError, AttributeError):
        return None
    values: dict[str, float] = {}
    for row in rows:
        iso2 = ISO3_TO_ISO2.get(str(row.get("reporterISO") or ""))
        value = row.get("primaryValue")
        if iso2 and isinstance(value, int | float) and value > 0 and str(row.get("partnerCode")) == "0":
            values[iso2] = values.get(iso2, 0.0) + float(value)
    total = sum(values.values())
    if total <= 0:
        return None
    shares = {k: round(100 * v / total, 2) for k, v in values.items()}
    return Reference(f"UN Comtrade {year}, exportations du code SH {hs6}",
                     f"https://comtradeplus.un.org/TradeFlow?Frequency=A&Flows=X&CommodityCodes={hs6}&Period={year}",
                     "exports", shares)


def _number(text: str) -> float | None:
    """A plain figure (« 900,000 »); USGS markers (W withheld, NA, —, ranges « 10,000 - 15,000 ») give None."""
    cleaned = (text or "").strip()
    if not NUMBER.match(cleaned):
        return None
    return float(cleaned.replace(",", ""))


def _commodity_matches(name: str, wanted: str) -> bool:
    """« graphite » matches « Graphite (Natural) », « niobium » matches « Niobium (Columbium) »."""
    name = name.strip().lower()
    return name == wanted or name.startswith(wanted + " (")


def _series(detail: str) -> str:
    return re.sub(r"[:,]\s*rounded$", "", detail.strip().lower()).strip()


def usgs_long_shares(rows: list[dict[str, str]], commodity: str, url: str, hint: str | None = None
                     ) -> Reference | None:
    """MCS 2026 table, one figure per row (Section, Commodity, Country, Statistics, Statistics_detail, Year, Value).
    World production only, latest year with a world total. A commodity with several production series is compared
    only when the node says which one (mine or refinery) and exactly one series matches: otherwise nothing is
    compared, rather than a mine share against a refinery total."""
    wanted = commodity.lower()
    by_series: dict[str, dict[str, dict[str, float]]] = {}  # series -> year -> country -> value
    for r in rows:
        if not (r.get("Section") or "").startswith("World") or (r.get("Statistics") or "").strip() != "Production":
            continue
        if not _commodity_matches(r.get("Commodity") or "", wanted):
            continue
        value = _number(r.get("Value") or "")
        year = (r.get("Year") or "").strip()
        if value is None or not year.isdigit():
            continue
        country = (r.get("Country") or "").strip().lower()
        by_series.setdefault(_series(r.get("Statistics_detail") or ""), {}).setdefault(year, {})[country] = value
    complete = {}
    for series, years in by_series.items():
        for year in sorted(years, reverse=True):
            values = years[year]
            world = next((v for k, v in values.items() if k.startswith("world total")), None)
            shares = {USGS_NAMES[k]: v for k, v in values.items() if k in USGS_NAMES}
            if world and shares:
                complete[series] = (year, world, shares)
                break
    if len(by_series) > 1:  # soda ash: natural, synthetic, both; copper: mine, refinery
        wanted_words = ("refinery", "smelter") if hint == "refinery" else ("mine",) if hint == "mine" else ()
        candidates = [s for s in by_series if any(w in s for w in wanted_words)]
        # Exactly one series of the table must match, complete or not: soda ash has three « mine » series.
        one = candidates[0] if len(candidates) == 1 else None
        complete = {one: complete[one]} if one in complete else {}
    if len(complete) != 1:
        return None
    series, (year, world, shares) = next(iter(complete.items()))
    label = series.removeprefix(f"{wanted}, ")  # « bauxite, mine production » → « mine production »
    return Reference(f"USGS Mineral Commodity Summaries, {commodity}, {label} {year}", url, "production",
                     {k: round(100 * v / world, 2) for k, v in shares.items()})


def usgs_production_shares(csv_text: str, commodity: str, url: str, hint: str | None = None) -> Reference | None:
    """Shares from the MCS world-production CSV: the 2026 table (one figure per row), or the earlier one (a
    `Prod_…` column per year: last column with a world total)."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return None
    if "Statistics" in reader.fieldnames and "Value" in reader.fieldnames:
        return usgs_long_shares(list(reader), commodity, url, hint)
    prod_cols = [c for c in reader.fieldnames if c and c.lower().startswith("prod")]
    wanted = commodity.lower()
    rows = []
    for raw in reader:
        r = {str(k or "").strip().upper(): v for k, v in raw.items()}
        name = (r.get("COMMODITY") or "").strip().lower()
        # « Bauxite and alumina » holds both: its TYPE column says which one a row is.
        kind = (r.get("TYPE") or "").strip().lower()
        if name == wanted or (wanted in name.split(" and ") and (not kind or wanted in kind)):
            rows.append(r)
    for col in reversed(prod_cols):
        col = col.strip().upper()
        world = None
        by_country: dict[str, float] = {}
        for r in rows:
            name = (r.get("COUNTRY") or "").strip().lower()
            value = _number(r.get(col) or "")
            if value is None:
                continue
            if name.startswith("world total"):
                world = value
            elif name in USGS_NAMES:
                by_country[USGS_NAMES[name]] = by_country.get(USGS_NAMES[name], 0.0) + value
        if world and by_country:
            return Reference(f"USGS Mineral Commodity Summaries ({col}), production de {commodity}", url,
                             "production", {k: round(100 * v / world, 2) for k, v in by_country.items()})
    return None


def apply_reference(chain: dict[str, Any], ref: Reference, *, tier: int | None = None,
                    product_word: str | None = None, commodity: str | None = None,
                    hint: str | None = None) -> int:
    """Compare country nodes to the reference; returns the number of nodes flagged."""
    flagged = 0
    for n in chain["nodes"]:
        if n["node_type"] != "country" or n["global_share_pct"] is None or n["country_iso2"] not in ref.shares:
            continue
        if tier is not None and n["tier"] != tier:
            continue
        if product_word and product_word.lower() not in n["product"].lower():
            continue
        if commodity and (usgs_commodity(f"{n['product']} {n['name']}") != commodity
                          or usgs_hint(f"{n['product']} {n['name']}") != hint):
            continue
        observed = ref.shares[n["country_iso2"]]
        claimed = float(n["global_share_pct"])
        if max(observed, claimed) == 0:
            continue
        gap = abs(claimed - observed) / max(observed, 0.01)
        n.setdefault("crosscheck", []).append({"source": ref.source, "url": ref.url, "share_pct": observed})
        basis = n.get("share_basis", "unknown")
        if basis not in ("unknown", ref.basis):
            continue  # production against exports: two different things, no gap to report
        if gap > MAX_RELATIVE_GAP:
            n["confidence"] = "low"
            n.setdefault("anomalies", []).append(
                f"Écart de plus de 30 % avec {ref.source} : {observed:.1f} % au lieu de {claimed:.1f} %."
            )
            flagged += 1
    return flagged
