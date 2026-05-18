from __future__ import annotations

import csv
from pathlib import Path

from valuechain.models import Company


def read_universe(path: Path, tickers: list[str] | None = None) -> list[Company]:
    wanted = {ticker.upper().strip() for ticker in tickers or [] if ticker.strip()}
    companies: list[Company] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ticker = row["ticker"].upper().strip()
            if wanted and ticker not in wanted:
                continue
            companies.append(
                Company(
                    ticker=ticker,
                    company_name=row.get("company_name", "").strip(),
                    role=row.get("role", "").strip(),
                    priority=int(row.get("priority") or 3),
                    notes=row.get("notes", "").strip(),
                )
            )
    return companies


def parse_tickers(tickers: str | None) -> list[str] | None:
    if not tickers:
        return None
    return [part.strip().upper() for part in tickers.split(",") if part.strip()]

