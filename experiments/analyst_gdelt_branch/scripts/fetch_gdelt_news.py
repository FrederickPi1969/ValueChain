from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx


ROOT = Path(__file__).resolve().parents[3]
BRANCH_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = BRANCH_DIR / "outputs" / "gdelt_news" / "gdelt_articles.jsonl"
DEFAULT_SCORECARD = BRANCH_DIR / "outputs" / "analyst_lens" / "industry-sec-exhibits-v3" / "company_scorecard.csv"
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
    "foundry",
    "HBM",
]
QUERY_MODE_CHOICES = {"company", "value_chain", "sec_object"}


@dataclass(frozen=True)
class CompanyQuery:
    ticker: str
    company_name: str
    query_name: str


@dataclass(frozen=True)
class QuerySpec:
    company: CompanyQuery
    mode: str
    query: str
    sec_object: str = ""


@dataclass(frozen=True)
class QueryResult:
    spec: QuerySpec
    rows: list[dict[str, Any]]


def main() -> int:
    args = parse_args()
    companies = select_companies(args.tickers, limit=args.limit_companies)
    start = parse_date(args.start)
    end = parse_date(args.end)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    specs = build_query_specs(
        companies=companies,
        modes=parse_modes(args.query_modes),
        scorecard_path=Path(args.scorecard),
        objects_per_company=args.objects_per_company,
    )
    articles = asyncio.run(
        fetch_all(
            specs=specs,
            start=start,
            end=end,
            max_records=args.max_records,
            concurrency=args.concurrency,
            min_interval=args.min_interval,
            request_timeout=args.timeout,
            retries=args.retries,
        )
    )
    articles = dedupe_articles(articles)
    with output.open("w", encoding="utf-8") as handle:
        for row in articles:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"Query specs={len(specs)}")
    print(f"Wrote {len(articles)} GDELT articles to {output}")
    for ticker in sorted({row['ticker'] for row in articles}):
        print(ticker, sum(1 for row in articles if row["ticker"] == ticker))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch local GDELT news overlay records.")
    parser.add_argument("--tickers", default="NVDA,AMD,CEG,DLR")
    parser.add_argument("--limit-companies", type=int, default=0)
    parser.add_argument("--start", default="2026-05-01")
    parser.add_argument("--end", default="2026-05-07")
    parser.add_argument("--max-records", type=int, default=75)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--min-interval", type=float, default=0.25)
    parser.add_argument("--timeout", type=float, default=35.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--query-modes", default="company,value_chain,sec_object")
    parser.add_argument("--objects-per-company", type=int, default=2)
    parser.add_argument("--scorecard", default=str(DEFAULT_SCORECARD))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args()


def parse_modes(value: str) -> list[str]:
    modes = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [mode for mode in modes if mode not in QUERY_MODE_CHOICES]
    if unknown:
        raise SystemExit(f"Unknown query modes: {unknown}")
    return modes


def build_query_specs(
    companies: list[CompanyQuery],
    modes: list[str],
    scorecard_path: Path,
    objects_per_company: int,
) -> list[QuerySpec]:
    scorecard_objects = read_scorecard_objects(scorecard_path)
    specs: list[QuerySpec] = []
    for company in companies:
        if "company" in modes:
            specs.append(QuerySpec(company=company, mode="company", query=f'"{company.query_name}"'))
        if "value_chain" in modes:
            specs.append(
                QuerySpec(
                    company=company,
                    mode="value_chain",
                    query=f'"{company.query_name}" ({' OR '.join(VALUE_CHAIN_TERMS)})',
                )
            )
        if "sec_object" in modes:
            for sec_object in scorecard_objects.get(company.ticker, [])[:objects_per_company]:
                specs.append(
                    QuerySpec(
                        company=company,
                        mode="sec_object",
                        query=f'"{company.query_name}" "{sec_object}"',
                        sec_object=sec_object,
                    )
                )
    return specs


async def fetch_all(
    specs: list[QuerySpec],
    start: date,
    end: date,
    max_records: int,
    concurrency: int,
    min_interval: float,
    request_timeout: float,
    retries: int,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    throttle = Throttle(min_interval)
    timeout = httpx.Timeout(request_timeout)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "ValueChain-GDELT-Experiment/0.2"}) as client:
        tasks = [
            asyncio.create_task(
                fetch_query(
                client=client,
                semaphore=semaphore,
                throttle=throttle,
                spec=spec,
                start=start,
                end=end,
                max_records=max_records,
                retries=max(1, retries),
                )
            )
            for spec in specs
        ]
        results: list[QueryResult] = []
        for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
            result = await task
            results.append(result)
            print(
                f"[{completed}/{len(tasks)}] {result.spec.company.ticker} "
                f"{result.spec.mode} rows={len(result.rows)}",
                flush=True,
            )
    return [row for result in results for row in result.rows]


async def fetch_query(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    throttle: "Throttle",
    spec: QuerySpec,
    start: date,
    end: date,
    max_records: int,
    retries: int,
) -> QueryResult:
    params = {
        "query": spec.query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(max_records),
        "startdatetime": f"{start:%Y%m%d}000000",
        "enddatetime": f"{end:%Y%m%d}235959",
        "sort": "datedesc",
    }
    url = f"{GDELT_URL}?{urlencode(params)}"
    for attempt in range(retries):
        try:
            async with semaphore:
                await throttle.wait()
                response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
            return QueryResult(spec=spec, rows=normalize_articles(spec, start, end, payload.get("articles") or []))
        except Exception as exc:
            if attempt == retries - 1:
                print(f"GDELT failed for {spec.company.ticker} {spec.mode}: {exc}")
                return QueryResult(spec=spec, rows=[])
            await asyncio.sleep((2**attempt) * 0.5 + random.random() * 0.25)
    return QueryResult(spec=spec, rows=[])


def normalize_articles(
    spec: QuerySpec,
    start: date,
    end: date,
    articles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for article in articles:
        language = str(article.get("language", ""))
        if language and language.lower() != "english":
            continue
        title = article.get("title", "")
        domain = article.get("domain", "")
        rows.append(
            {
                "ticker": spec.company.ticker,
                "company_name": spec.company.company_name,
                "query_name": spec.company.query_name,
                "query_mode": spec.mode,
                "sec_object": spec.sec_object,
                "query": spec.query,
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
                "title": title,
                "canonical_title": canonical_title(title),
                "url": article.get("url", ""),
                "domain": domain,
                "source_country": article.get("sourcecountry", ""),
                "language": language,
                "seendate": article.get("seendate", ""),
                "socialimage": article.get("socialimage", ""),
            }
        )
    return rows


def select_companies(tickers_arg: str, limit: int = 0) -> list[CompanyQuery]:
    wanted = [ticker.strip().upper() for ticker in tickers_arg.split(",") if ticker.strip()]
    rows = read_universe()
    by_ticker = {row["ticker"].upper(): row for row in rows}
    if not wanted:
        wanted = [row["ticker"].upper() for row in rows]
    if limit > 0:
        wanted = wanted[:limit]
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


def read_scorecard_objects(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    objects: dict[str, list[str]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = row.get("ticker", "")
            raw = row.get("top_named_counterparties") or row.get("top_dependency_objects") or ""
            selected = [item.strip() for item in raw.split(";") if is_queryable_object(item.strip())]
            objects[ticker] = selected
    return objects


def is_queryable_object(value: str) -> bool:
    lower = value.lower()
    if not value or len(value) < 3:
        return False
    if "class" in lower or lower in {"china", "united states", "taiwan", "europe", "asia"}:
        return False
    if lower.startswith(("contents ", "notes ", "item ", "table ")):
        return False
    return True


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
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row.get("ticker", ""), row.get("url") or row.get("canonical_title", ""))
        if key not in by_key:
            by_key[key] = row
            by_key[key]["query_modes"] = row.get("query_mode", "")
            by_key[key]["sec_objects"] = row.get("sec_object", "")
            continue
        existing = by_key[key]
        existing["query_modes"] = merge_semicolon(existing.get("query_modes", ""), row.get("query_mode", ""))
        existing["sec_objects"] = merge_semicolon(existing.get("sec_objects", ""), row.get("sec_object", ""))
    return list(by_key.values())


def merge_semicolon(left: str, right: str) -> str:
    values = [item for item in left.split(";") if item] + [item for item in right.split(";") if item]
    return ";".join(sorted(set(values)))


def canonical_title(title: str) -> str:
    lowered = title.lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


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
