"""Price series from free sources, called by the allowlist client only (docs/SPEC.md, section 6; CLAUDE.md, rule 6).

Each reader returns `(day, value)` points, or `(day, value, volume)` for customs data. Keys never sit in a log:
EIA and Census put them in the query string (redacted by the log filter), BLS in the POST body.
Not wired yet: the Chinese exchanges and the NBS ten-day prices (pages to check from the Mac), and the World Bank
monthly file (an .xlsx is a ZIP archive, and no archive from a source is decompressed: docs/SECURITY.md, threat 11).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime
from typing import Any

import httpx2

from goulot.net.client import fetch, post_json

Point = tuple[date, float]

EIA_ROUTES = {"RBRTE": "petroleum/pri/spt", "RWTC": "petroleum/pri/spt", "RNGWHHD": "natural-gas/pri/fut"}
EIA = "https://api.eia.gov/v2/{route}/data/"
FRED = "https://api.stlouisfed.org/fred/series/observations"
BLS = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
CENSUS = "https://api.census.gov/data/timeseries/intltrade/imports/hs"
EUROSTAT = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
STOOQ = "https://stooq.com/q/d/l/"


class PriceSourceError(RuntimeError):
    pass


def _ok_json(result: Any) -> Any:
    if result.status != 200:
        raise PriceSourceError(f"HTTP {result.status}")
    try:
        return json.loads(result.content)
    except json.JSONDecodeError as exc:
        raise PriceSourceError("invalid JSON") from exc


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN is not a price


def eia(client: httpx2.Client, key: str, series: str, start: date) -> list[Point]:
    route = EIA_ROUTES.get(series)
    if route is None:
        raise PriceSourceError("unknown EIA series")
    data = _ok_json(fetch(client, EIA.format(route=route), params={
        "api_key": key, "frequency": "daily", "data[0]": "value", "facets[series][]": series,
        "start": start.isoformat(), "sort[0][column]": "period", "sort[0][direction]": "asc", "length": "5000",
    }))
    out = []
    for row in (data.get("response") or {}).get("data") or []:
        v = _num(row.get("value"))
        if v is not None and row.get("period"):
            out.append((date.fromisoformat(str(row["period"])[:10]), v))
    return out


def fred(client: httpx2.Client, key: str, series: str, start: date) -> list[Point]:
    data = _ok_json(fetch(client, FRED, params={
        "series_id": series, "api_key": key, "file_type": "json", "observation_start": start.isoformat()}))
    out = []
    for row in data.get("observations") or []:
        v = _num(row.get("value"))  # FRED writes « . » for a missing value
        if v is not None:
            out.append((date.fromisoformat(row["date"]), v))
    return out


def bls(client: httpx2.Client, key: str | None, series: list[str], start_year: int, end_year: int
        ) -> dict[str, list[Point]]:
    payload: dict[str, object] = {"seriesid": series[:50], "startyear": str(start_year), "endyear": str(end_year)}
    if key:
        payload["registrationkey"] = key
    data = _ok_json(post_json(client, BLS, payload))
    if data.get("status") not in ("REQUEST_SUCCEEDED", None):
        raise PriceSourceError("BLS request not succeeded")
    out: dict[str, list[Point]] = {}
    for s in (data.get("Results") or {}).get("series") or []:
        points = []
        for row in s.get("data") or []:
            period, v = str(row.get("period", "")), _num(row.get("value"))
            if period.startswith("M") and period != "M13" and v is not None:
                points.append((date(int(row["year"]), int(period[1:]), 1), v))
        out[str(s.get("seriesID"))] = sorted(points)
    return out


def census_unit_prices(client: httpx2.Client, key: str | None, hs6: str, start: date
                       ) -> list[tuple[date, float, float]]:
    """US imports of an HS6 code, all origins: monthly value / quantity, with the quantity as volume."""
    params = {"get": "GEN_VAL_MO,GEN_QY1_MO,UNIT_QY1,CTY_CODE", "COMM_LVL": "HS6", "I_COMMODITY": hs6,
              "time": f"from {start:%Y-%m}"}
    if key:
        params["key"] = key
    rows = _ok_json(fetch(client, CENSUS, params=params))
    if not isinstance(rows, list) or len(rows) < 2:
        return []
    head = rows[0]
    idx = {name: head.index(name) for name in ("GEN_VAL_MO", "GEN_QY1_MO", "CTY_CODE", "time") if name in head}
    totals: dict[date, list[float]] = {}
    by_country: dict[date, list[float]] = {}
    for r in rows[1:]:
        value, qty = _num(r[idx["GEN_VAL_MO"]]), _num(r[idx["GEN_QY1_MO"]])
        if value is None or not qty:
            continue
        month = date.fromisoformat(str(r[idx["time"]]) + "-01")
        code = str(r[idx["CTY_CODE"]]) if "CTY_CODE" in idx else "-"
        if code == "-":
            totals.setdefault(month, [0.0, 0.0])
            totals[month][0] += value
            totals[month][1] += qty
        elif code.isdigit() and int(code) >= 1000:  # country codes; 0001–0026 are regional aggregates
            by_country.setdefault(month, [0.0, 0.0])
            by_country[month][0] += value
            by_country[month][1] += qty
    source = totals or by_country
    return sorted((m, v / q, q) for m, (v, q) in source.items() if q > 0)


def eurostat(client: httpx2.Client, dataset: str, filters: dict[str, str]) -> list[Point]:
    """JSON-stat 2.0 with every dimension but time fixed by `filters`."""
    data = _ok_json(fetch(client, EUROSTAT.format(dataset=dataset), params={"format": "JSON", "lang": "EN",
                                                                              **filters}))
    dims = data.get("id") or []
    sizes = data.get("size") or []
    if "time" not in dims or any(s != 1 for d, s in zip(dims, sizes, strict=False) if d != "time"):
        raise PriceSourceError("ambiguous JSON-stat dimensions")
    periods = data["dimension"]["time"]["category"]["index"]
    values = data.get("value") or {}
    out = []
    for period, i in periods.items():
        v = _num(values.get(str(i)) if isinstance(values, dict) else (values[i] if i < len(values) else None))
        if v is not None and len(period) == 7:  # 2025-08 (monthly); « 2025M08 » in some datasets
            out.append((date.fromisoformat(period + "-01"), v))
        elif v is not None and "M" in period:
            year, month = period.split("M")
            out.append((date(int(year), int(month), 1), v))
    return sorted(out)


def yahoo(client: httpx2.Client, symbol: str, range_: str = "10y") -> list[Point]:
    data = _ok_json(fetch(client, YAHOO.format(symbol=symbol), params={"range": range_, "interval": "1d"}))
    result = ((data.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise PriceSourceError("no chart result")
    stamps = result.get("timestamp") or []
    closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out = []
    for t, c in zip(stamps, closes, strict=False):
        v = _num(c)
        if v is not None:
            out.append((datetime.fromtimestamp(int(t), UTC).date(), v))
    return out


def stooq(client: httpx2.Client, symbol: str) -> list[Point]:
    result = fetch(client, STOOQ, params={"s": symbol, "i": "d"})
    if result.status != 200:
        raise PriceSourceError(f"HTTP {result.status}")
    out = []
    for row in csv.DictReader(io.StringIO(result.content.decode("utf-8", "replace"))):
        v = _num(row.get("Close"))
        if v is not None and row.get("Date"):
            try:
                out.append((date.fromisoformat(row["Date"]), v))
            except ValueError:
                continue
    return out
