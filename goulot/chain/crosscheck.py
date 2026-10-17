"""Step 3: minimal cross-check of the country shares (docs/SPEC.md, section 3).

- UN Comtrade: export shares of the purchased material's HS code, latest complete year, compared with the
  tier-1 country nodes.
- USGS Mineral Commodity Summaries (world production table of the data release, CSV), compared with the
  country nodes of the same commodity. Used only when `data_url` of `usgs_mcs` is set in `config/sources.yaml`.
A relative gap above 30 % drops the node to low confidence with a comment. Missing data changes nothing.
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
}


@dataclass
class Reference:
    source: str  # "UN Comtrade 2024 (exportations)" or "USGS MCS (production)"
    url: str
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
                     shares)


def _number(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", text or "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def usgs_production_shares(csv_text: str, commodity: str, url: str) -> Reference | None:
    """Shares from the MCS world-production CSV: last `Prod_…` column with a world total."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return None
    prod_cols = [c for c in reader.fieldnames if c and c.lower().startswith("prod")]
    rows = [r for r in reader if (r.get("COMMODITY") or r.get("Commodity") or "").strip().lower() == commodity.lower()]
    for col in reversed(prod_cols):
        world = None
        by_country: dict[str, float] = {}
        for r in rows:
            name = (r.get("COUNTRY") or r.get("Country") or "").strip().lower()
            value = _number(r.get(col) or "")
            if value is None:
                continue
            if name.startswith("world total"):
                world = value
            elif name in USGS_NAMES:
                by_country[USGS_NAMES[name]] = by_country.get(USGS_NAMES[name], 0.0) + value
        if world and by_country:
            return Reference(f"USGS Mineral Commodity Summaries ({col}), production de {commodity}", url,
                             {k: round(100 * v / world, 2) for k, v in by_country.items()})
    return None


def apply_reference(chain: dict[str, Any], ref: Reference, *, tier: int | None = None,
                    product_word: str | None = None) -> int:
    """Compare country nodes to the reference; returns the number of nodes flagged."""
    flagged = 0
    for n in chain["nodes"]:
        if n["node_type"] != "country" or n["global_share_pct"] is None or n["country_iso2"] not in ref.shares:
            continue
        if tier is not None and n["tier"] != tier:
            continue
        if product_word and product_word.lower() not in n["product"].lower():
            continue
        observed = ref.shares[n["country_iso2"]]
        claimed = float(n["global_share_pct"])
        if max(observed, claimed) == 0:
            continue
        gap = abs(claimed - observed) / max(observed, 0.01)
        n.setdefault("crosscheck", []).append({"source": ref.source, "url": ref.url, "share_pct": observed})
        if gap > MAX_RELATIVE_GAP:
            n["confidence"] = "low"
            n.setdefault("anomalies", []).append(
                f"Écart de plus de 30 % avec {ref.source} : {observed:.1f} % au lieu de {claimed:.1f} %."
            )
            flagged += 1
    return flagged
