from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx


ROOT = Path(__file__).resolve().parents[3]
BRANCH_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = BRANCH_DIR / "outputs" / "gdelt_news" / "gdelt_articles.jsonl"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
VALUE_CHAIN_TERMS = [
    "AI",
    '"artificial intelligence"',
    "GPU",
    "semiconductor",
    "chip",
    '"data center"',
    "datacenter",
    "cloud",
    "power",
    "grid",
]


@dataclass(frozen=True)
class CompanyQuery:
    ticker: str
    company_name: str
    query_name: str


def main() -> int:
    args = parse_args()
    companies = select_companies(args.tickers)
    start = parse_date(args.start)
    end = parse_date(args.end)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    articles = asyncio.run(
        fetch_all(
            companies=companies,
            start=start,
            end=end,
            max_records=args.max_records,
            concurrency=args.concurrency,
            min_interval=args.min_interval,
            request_timeout=args.timeout,
            value_chain_filter=not args.company_only,
        )
    )
    articles = dedupe_articles(articles)
    with output.open("w", encoding="utf-8") as handle:
        for row in articles:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"Wrote {len(articles)} GDELT articles to {output}")
    for ticker in sorted({row['ticker'] for row in articles}):
        print(ticker, sum(1 for row in articles if row["ticker"] == ticker))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch local GDELT news overlay records.")
    parser.add_argument("--tickers", default="NVDA,AMD,CEG,DLR")
    parser.add_argument("--start", default="2026-05-01")
    parser.add_argument("--end", default="2026-05-07")
    parser.add_argument("--max-records", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--min-interval", type=float, default=0.4)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--company-only", action="store_true")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args()


async def fetch_all(
    companies: list[CompanyQuery],
    start: date,
    end: date,
    max_records: int,
    concurrency: int,
    min_interval: float,
    request_timeout: float,
    value_chain_filter: bool,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    throttle = Throttle(min_interval)
    timeout = httpx.Timeout(request_timeout)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "ValueChain-GDELT-Experiment/0.1"}) as client:
        tasks = [
            fetch_company(
                client=client,
                semaphore=semaphore,
                throttle=throttle,
                company=company,
                start=start,
                end=end,
                max_records=max_records,
                value_chain_filter=value_chain_filter,
            )
            for company in companies
        ]
        batches = await asyncio.gather(*tasks)
    return [row for batch in batches for row in batch]


async def fetch_company(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    throttle: "Throttle",
    company: CompanyQuery,
    start: date,
    end: date,
    max_records: int,
    value_chain_filter: bool,
) -> list[dict[str, Any]]:
    query = build_query(company.query_name, value_chain_filter=value_chain_filter)
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(max_records),
        "startdatetime": f"{start:%Y%m%d}000000",
        "enddatetime": f"{end:%Y%m%d}235959",
        "sort": "datedesc",
    }
    url = f"{GDELT_URL}?{urlencode(params)}"
    for attempt in range(5):
        try:
            async with semaphore:
                await throttle.wait()
                response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
            return normalize_articles(company, query, start, end, payload.get("articles") or [])
        except Exception as exc:
            if attempt == 4:
                print(f"GDELT failed for {company.ticker}: {exc}")
                return []
            await asyncio.sleep((2**attempt) * 0.5 + random.random() * 0.25)
    return []


def normalize_articles(
    company: CompanyQuery,
    query: str,
    start: date,
    end: date,
    articles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for article in articles:
        language = str(article.get("language", ""))
        if language and language.lower() != "english":
            continue
        rows.append(
            {
                "ticker": company.ticker,
                "company_name": company.company_name,
                "query_name": company.query_name,
                "query": query,
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
                "title": article.get("title", ""),
                "url": article.get("url", ""),
                "domain": article.get("domain", ""),
                "source_country": article.get("sourcecountry", ""),
                "language": language,
                "seendate": article.get("seendate", ""),
                "socialimage": article.get("socialimage", ""),
            }
        )
    return rows


def build_query(company_name: str, value_chain_filter: bool) -> str:
    quoted = f'"{company_name}"'
    if not value_chain_filter:
        return quoted
    return f"{quoted} ({' OR '.join(VALUE_CHAIN_TERMS)})"


def select_companies(tickers_arg: str) -> list[CompanyQuery]:
    wanted = [ticker.strip().upper() for ticker in tickers_arg.split(",") if ticker.strip()]
    rows = read_universe()
    by_ticker = {row["ticker"].upper(): row for row in rows}
    selected: list[CompanyQuery] = []
    for ticker in wanted:
        row = by_ticker.get(ticker)
        if not row:
            raise SystemExit(f"Unknown ticker in universe: {ticker}")
        selected.append(
            CompanyQuery(
                ticker=ticker,
                company_name=row["company_name"],
                query_name=simplify_query_name(row["company_name"]),
            )
        )
    return selected


def read_universe() -> list[dict[str, str]]:
    path = ROOT / "data" / "universe" / "ai_infra_universe.csv"
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def simplify_query_name(company_name: str) -> str:
    replacements = [
        " Corporation",
        " Incorporated",
        " Inc.",
        " Inc",
        " Corp.",
        " Corp",
        " plc",
        " N.V.",
        " Ltd.",
        " Ltd",
        " Limited",
    ]
    name = company_name
    for suffix in replacements:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if name == "Alphabet":
        return "Google"
    if name == "Amazon.com":
        return "Amazon Web Services"
    return name


def dedupe_articles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        key = (row.get("ticker", ""), row.get("url", "") or row.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


class Throttle:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def wait(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = asyncio.get_event_loop().time()
            delay = self.min_interval - (now - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = asyncio.get_event_loop().time()


if __name__ == "__main__":
    raise SystemExit(main())

