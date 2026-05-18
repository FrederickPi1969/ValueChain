from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from valuechain.models import Company


def read_universe(
    path: Path,
    tickers: list[str] | None = None,
    roles: list[str] | None = None,
    max_priority: int | None = None,
    limit: int | None = None,
) -> list[Company]:
    companies: list[Company] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            companies.append(
                Company(
                    ticker=row["ticker"].upper().strip(),
                    company_name=row.get("company_name", "").strip(),
                    role=row.get("role", "").strip(),
                    priority=int(row.get("priority") or 3),
                    notes=row.get("notes", "").strip(),
                )
            )
    return filter_universe(
        companies,
        tickers=tickers,
        roles=roles,
        max_priority=max_priority,
        limit=limit,
    )


def filter_universe(
    companies: list[Company],
    tickers: list[str] | None = None,
    roles: list[str] | None = None,
    max_priority: int | None = None,
    limit: int | None = None,
) -> list[Company]:
    wanted_tickers = {ticker.upper().strip() for ticker in tickers or [] if ticker.strip()}
    wanted_roles = {role.strip().lower() for role in roles or [] if role.strip()}
    filtered: list[Company] = []
    for company in companies:
        if wanted_tickers and company.ticker.upper() not in wanted_tickers:
            continue
        if wanted_roles and company.role.lower() not in wanted_roles:
            continue
        if max_priority is not None and company.priority > max_priority:
            continue
        filtered.append(company)
    filtered.sort(key=lambda item: (item.priority, item.role, item.ticker))
    return filtered[:limit] if limit else filtered


def parse_tickers(tickers: str | None) -> list[str] | None:
    if not tickers:
        return None
    return [part.strip().upper() for part in tickers.split(",") if part.strip()]


def parse_csv_arg(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def summarize_universe(companies: list[Company]) -> dict[str, Any]:
    role_counts = Counter(company.role for company in companies)
    priority_counts = Counter(str(company.priority) for company in companies)
    return {
        "company_count": len(companies),
        "tickers": [company.ticker for company in companies],
        "role_counts": dict(sorted(role_counts.items())),
        "priority_counts": dict(sorted(priority_counts.items())),
    }
