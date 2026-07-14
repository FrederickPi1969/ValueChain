from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from valuechain.config import ROOT


CATALOG_ROOT = ROOT / "config" / "curated_markets"


@dataclass(frozen=True)
class CuratedCompany:
    ticker: str
    company_name: str
    tier: int
    theme: str

    def metadata(self) -> dict[str, str | int | bool]:
        return {
            "curated_watchlist": True,
            "watchlist_tier": self.tier,
            "watchlist_theme": self.theme,
            "watchlist_name": self.company_name,
        }


def load_curated_companies(market: str) -> tuple[CuratedCompany, ...]:
    path = CATALOG_ROOT / f"{market.lower()}.csv"
    if not path.is_file():
        raise FileNotFoundError(f"Curated market catalog not found: {path}")
    companies: list[CuratedCompany] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            ticker = str(row.get("ticker") or "").strip()
            company_name = str(row.get("company_name") or "").strip()
            theme = str(row.get("theme") or "").strip()
            if not ticker or not company_name or not theme:
                raise ValueError(f"Incomplete curated company at {path}:{row_number}")
            if ticker in seen:
                raise ValueError(f"Duplicate ticker {ticker} at {path}:{row_number}")
            seen.add(ticker)
            tier = int(row.get("tier") or 0)
            if tier not in {1, 2}:
                raise ValueError(f"Unsupported tier {tier} at {path}:{row_number}")
            companies.append(CuratedCompany(ticker, company_name, tier, theme))
    return tuple(companies)
