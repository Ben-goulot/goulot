"""Shanghai Futures Exchange and its INE platform: one JSON file per trading day
(`/data/tradedata/future/dailydata/kx<YYYYMMDD>.dat`) with every contract of SHFE and INE. Download and storage:
`exchange_files.py`.
"""

from __future__ import annotations

import json

from goulot.prices.exchange_files import Exchange, Product, UnreadableFile, dominant

PRODUCTS = (
    Product("cu", "Cuivre (SHFE)", "7440-50-8", "CNY/t", ("cuivre", "copper")),
    Product("bc", "Cuivre international (INE)", "7440-50-8", "CNY/t"),
    Product("al", "Aluminium (SHFE)", "7429-90-5", "CNY/t", ("aluminium",)),
    Product("ao", "Alumine (SHFE)", "1344-28-1", "CNY/t", ("alumine", "alumina")),
    Product("zn", "Zinc (SHFE)", "7440-66-6", "CNY/t", ("zinc",)),
    Product("pb", "Plomb (SHFE)", "7439-92-1", "CNY/t", ("plomb",)),
    Product("ni", "Nickel (SHFE)", "7440-02-0", "CNY/t", ("nickel",)),
    Product("sn", "Étain (SHFE)", "7440-31-5", "CNY/t", ("etain",)),
    Product("au", "Or (SHFE)", "7440-57-5", "CNY/g"),
    Product("ag", "Argent (SHFE)", "7440-22-4", "CNY/kg"),
    Product("rb", "Rond à béton (SHFE)", None, "CNY/t"),
    Product("wr", "Fil machine (SHFE)", None, "CNY/t"),
    Product("hc", "Bobine laminée à chaud (SHFE)", None, "CNY/t"),
    Product("ss", "Acier inoxydable (SHFE)", None, "CNY/t"),
    Product("ru", "Caoutchouc naturel (SHFE)", None, "CNY/t", ("caoutchouc naturel",)),
    Product("nr", "Caoutchouc naturel TSR 20 (INE)", None, "CNY/t"),
    Product("br", "Caoutchouc butadiène (SHFE)", "9003-17-2", "CNY/t", ("caoutchouc butadiene", "polybutadiene")),
    Product("bu", "Bitume (SHFE)", "8052-42-4", "CNY/t", ("bitume",)),
    Product("fu", "Fioul (SHFE)", None, "CNY/t", ("fioul",)),
    Product("lu", "Fioul à basse teneur en soufre (INE)", None, "CNY/t"),
    Product("sc", "Pétrole brut (INE)", None, "CNY/bbl"),
    Product("sp", "Pâte à papier (SHFE)", None, "CNY/t"),
)
CODES = {p.code for p in PRODUCTS}


def parse_day(text: str) -> dict[str, float]:
    """{product code: settlement price of its dominant contract} from one daily file."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnreadableFile("invalid JSON") from exc
    rows = []
    for row in data.get("o_curinstrument") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("PRODUCTID", "")).strip().removesuffix("_f")
        month = str(row.get("DELIVERYMONTH", "")).strip()
        if code not in CODES or not (month.isdigit() and len(month) == 4):
            continue
        try:
            rows.append((code, float(row["SETTLEMENTPRICE"]), float(row["OPENINTEREST"])))
        except (KeyError, TypeError, ValueError):
            continue
    return dominant(rows)


EXCHANGE = Exchange("shfe_ine", "https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{ymd}.dat", PRODUCTS,
                    parse_day)
