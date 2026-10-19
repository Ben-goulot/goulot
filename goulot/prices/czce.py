"""Zhengzhou Commodity Exchange: one text file per trading day
(`/cn/DFSStaticFiles/Future/<YYYY>/<YYYYMMDD>/FutureDataDaily.txt`), one row per contract, columns separated by « | »
(contract, previous settlement, open, high, low, close, settlement, change 1, change 2, volume, open interest…).
The home page answers HTTP 412 to automated requests, but the daily file is public (checked from the Mac on
19 October 2026). Download and storage: `exchange_files.py`.
"""

from __future__ import annotations

import re

from goulot.prices.exchange_files import Exchange, Product, UnreadableFile, dominant

PRODUCTS = (
    Product("MA", "Méthanol (Zhengzhou)", "67-56-1", "CNY/t", ("methanol",)),
    Product("TA", "PTA (Zhengzhou)", "100-21-0", "CNY/t", ("pta", "acide terephtalique", "terephthalic acid")),
    Product("PX", "Paraxylène (Zhengzhou)", "106-42-3", "CNY/t", ("paraxylene", "p xylene")),
    Product("PL", "Propylène (Zhengzhou)", "115-07-1", "CNY/t", ("propylene",)),
    Product("UR", "Urée (Zhengzhou)", "57-13-6", "CNY/t", ("uree", "urea")),
    Product("SA", "Carbonate de soude (Zhengzhou)", "497-19-8", "CNY/t",
            ("carbonate de soude", "carbonate de sodium", "soda ash", "soude solvay")),
    Product("SH", "Soude caustique (Zhengzhou)", "1310-73-2", "CNY/t",
            ("soude caustique", "hydroxyde de sodium", "caustic soda", "sodium hydroxide")),
    Product("FG", "Verre plat (Zhengzhou)", None, "CNY/t"),
    Product("SF", "Ferrosilicium (Zhengzhou)", None, "CNY/t", ("ferrosilicium",)),
    Product("SM", "Silicomanganèse (Zhengzhou)", None, "CNY/t", ("silicomanganese",)),
    Product("PF", "Fibre polyester (Zhengzhou)", None, "CNY/t"),
    Product("PR", "PET bouteille (Zhengzhou)", None, "CNY/t"),
)
CODES = {p.code for p in PRODUCTS}
CONTRACT = re.compile(r"^([A-Z]{2})(\d{3})$")


def _number(text: str) -> float:
    return float(text.replace(",", "").strip())


def parse_day(text: str) -> dict[str, float]:
    """{product code: settlement price of its dominant contract} from one daily file."""
    if "|" not in text:
        raise UnreadableFile("not a daily file")
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 11:
            continue
        m = CONTRACT.match(cells[0])
        if not m or m.group(1) not in CODES:
            continue
        try:
            rows.append((m.group(1), _number(cells[6]), _number(cells[10])))
        except ValueError:
            continue
    return dominant(rows)


EXCHANGE = Exchange("czce", "https://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{ymd}/FutureDataDaily.txt",
                    PRODUCTS, parse_day)
