from __future__ import annotations

from typing import Any

import yfinance as yf

from valuechain.models import Company


YAHOO_FIELDS = [
    "symbol",
    "shortName",
    "sector",
    "industry",
    "marketCap",
    "currentPrice",
    "beta",
    "trailingPE",
    "forwardPE",
    "revenueGrowth",
    "grossMargins",
    "profitMargins",
    "enterpriseValue",
]


def fetch_yahoo_snapshot(companies: list[Company]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for company in companies:
        row = {field: "" for field in YAHOO_FIELDS}
        row["symbol"] = company.ticker
        try:
            info = yf.Ticker(company.ticker).get_info()
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
            continue
        for field in YAHOO_FIELDS:
            row[field] = info.get(field, row.get(field, ""))
        row["role"] = company.role
        row["company_name"] = company.company_name
        rows.append(row)
    return rows

