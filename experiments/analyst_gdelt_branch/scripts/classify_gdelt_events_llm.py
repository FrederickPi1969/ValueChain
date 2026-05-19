from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import httpx


BRANCH_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = BRANCH_DIR / "outputs" / "gdelt_news_large" / "gdelt_articles_annotated.jsonl"
DEFAULT_OUTPUT = BRANCH_DIR / "outputs" / "gdelt_news_large" / "event_frames_llm.jsonl"
DEFAULT_BASE_URL = "http://192.168.50.18:31969/v1"
DEFAULT_API_KEY = "1969"
DEFAULT_MODEL = "Qwen/Qwen3.5-4B"

EVENT_TYPES = [
    "demand_capex",
    "datacenter_power",
    "semiconductor_supply",
    "cloud_platform",
    "manufacturing_capacity",
    "partnership_contract",
    "regulation_geopolitics",
    "earnings_guidance",
    "customer_concentration",
    "market_price_action",
    "low_signal_noise",
    "other",
]


def main() -> int:
    args = parse_args()
    articles = read_jsonl(Path(args.input))
    selected = select_articles(
        articles,
        limit=args.limit,
        per_ticker=args.per_ticker,
        min_event_score=args.min_event_score,
        include_low_signal=args.include_low_signal,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    frames = asyncio.run(
        classify_all(
            articles=selected,
            base_url=args.base_url.rstrip("/"),
            api_key=args.api_key,
            model=args.model,
            concurrency=args.concurrency,
            timeout=args.timeout,
        )
    )
    write_jsonl(output, frames)
    write_summary(output.with_suffix(".summary.csv"), frames)
    write_summary_json(output.with_suffix(".summary.json"), frames)
    print(f"Selected {len(selected)} articles from {args.input}")
    print(f"Wrote {len(frames)} LLM event frames to {output}")
    for row in top_summary_rows(frames)[:15]:
        print(row["ticker"], row["article_count"], row["avg_materiality"], row["event_type_mix"])
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify GDELT headlines into value-chain event frames with Local LLM.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--per-ticker", type=int, default=8)
    parser.add_argument("--min-event-score", type=float, default=3.0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument("--include-low-signal", action="store_true")
    return parser.parse_args()


def select_articles(
    rows: list[dict[str, Any]],
    limit: int,
    per_ticker: int,
    min_event_score: float,
    include_low_signal: bool,
) -> list[dict[str, Any]]:
    ranked = sorted(
        rows,
        key=lambda row: (
            -float(row.get("event_relevance_score") or 0),
            str(row.get("ticker", "")),
            str(row.get("title", "")),
        ),
    )
    counts: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []
    for row in ranked:
        ticker = str(row.get("ticker", ""))
        if counts[ticker] >= per_ticker:
            continue
        if float(row.get("event_relevance_score") or 0) < min_event_score:
            continue
        quality = str(row.get("headline_quality_flag", ""))
        if not include_low_signal and quality in {"holdings_filing", "low_signal_domain", "empty"}:
            continue
        selected.append(row)
        counts[ticker] += 1
        if len(selected) >= limit:
            break
    return selected


async def classify_all(
    articles: list[dict[str, Any]],
    base_url: str,
    api_key: str,
    model: str,
    concurrency: int,
    timeout: float,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    ) as client:
        tasks = [
            classify_article(
                client=client,
                semaphore=semaphore,
                base_url=base_url,
                model=model,
                article=article,
            )
            for article in articles
        ]
        return await asyncio.gather(*tasks)


async def classify_article(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    base_url: str,
    model: str,
    article: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": json.dumps(article_payload(article), ensure_ascii=False, sort_keys=True)},
        ],
        "temperature": 0,
        "max_tokens": 420,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    for attempt in range(4):
        try:
            async with semaphore:
                response = await client.post(f"{base_url}/chat/completions", json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            frame = parse_json_object(content)
            return normalize_frame(article, frame)
        except Exception as exc:
            if attempt == 3:
                return failure_frame(article, str(exc))
            await asyncio.sleep((2**attempt) * 0.6 + random.random() * 0.2)
    return failure_frame(article, "unknown_error")


def system_prompt() -> str:
    return (
        "You are a financial NLP event classifier for an AI infrastructure value-chain monitor. "
        "Use only the supplied headline, domain, company, SEC-derived query context, and heuristic tags. "
        "Do not infer facts that are not in the input. Return one compact JSON object only. "
        f"event_type must be one of: {', '.join(EVENT_TYPES)}. "
        "materiality_score and value_chain_relevance_score are integers 0-3. "
        "direction must be beneficial, adverse, mixed, neutral, or unknown. "
        "confidence_score is 0-1. "
        "dependency_object should be a named counterparty, infrastructure class, or geography if explicit; otherwise empty. "
        "event_summary must be <= 20 words."
    )


def article_payload(article: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "ticker",
        "company_name",
        "title",
        "domain",
        "seendate",
        "primary_theme",
        "themes",
        "source_tier",
        "query_modes",
        "sec_objects",
        "event_relevance_score",
        "headline_quality_flag",
    ]
    return {key: article.get(key, "") for key in keys}


def parse_json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object in response: {content[:120]}")
    return json.loads(match.group(0))


def normalize_frame(article: dict[str, Any], frame: dict[str, Any]) -> dict[str, Any]:
    event_type = str(frame.get("event_type", "other"))
    if event_type not in EVENT_TYPES:
        event_type = "other"
    return {
        "ticker": article.get("ticker", ""),
        "company_name": article.get("company_name", ""),
        "title": article.get("title", ""),
        "url": article.get("url", ""),
        "domain": article.get("domain", ""),
        "seendate": article.get("seendate", ""),
        "heuristic_primary_theme": article.get("primary_theme", ""),
        "heuristic_event_relevance_score": article.get("event_relevance_score", 0),
        "query_modes": article.get("query_modes", ""),
        "sec_objects": article.get("sec_objects", ""),
        "event_type": event_type,
        "materiality_score": clamp_int(frame.get("materiality_score"), 0, 3),
        "value_chain_relevance_score": clamp_int(frame.get("value_chain_relevance_score"), 0, 3),
        "direction": normalize_direction(frame.get("direction")),
        "dependency_object": str(frame.get("dependency_object", ""))[:160],
        "event_summary": str(frame.get("event_summary", ""))[:220],
        "confidence_score": clamp_float(frame.get("confidence_score"), 0.0, 1.0),
        "llm_error": "",
    }


def failure_frame(article: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        "ticker": article.get("ticker", ""),
        "company_name": article.get("company_name", ""),
        "title": article.get("title", ""),
        "url": article.get("url", ""),
        "domain": article.get("domain", ""),
        "seendate": article.get("seendate", ""),
        "heuristic_primary_theme": article.get("primary_theme", ""),
        "heuristic_event_relevance_score": article.get("event_relevance_score", 0),
        "query_modes": article.get("query_modes", ""),
        "sec_objects": article.get("sec_objects", ""),
        "event_type": "other",
        "materiality_score": 0,
        "value_chain_relevance_score": 0,
        "direction": "unknown",
        "dependency_object": "",
        "event_summary": "",
        "confidence_score": 0.0,
        "llm_error": error[:500],
    }


def write_summary(path: Path, frames: list[dict[str, Any]]) -> None:
    rows = top_summary_rows(frames)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted({key for row in rows for key in row.keys()}))
        writer.writeheader()
        writer.writerows(rows)


def write_summary_json(path: Path, frames: list[dict[str, Any]]) -> None:
    payload = {
        "article_count": len(frames),
        "company_count": len({row["ticker"] for row in frames}),
        "error_count": sum(1 for row in frames if row.get("llm_error")),
        "event_type_counts": dict(Counter(row["event_type"] for row in frames).most_common()),
        "top_companies": top_summary_rows(frames)[:25],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def top_summary_rows(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for frame in frames:
        grouped[str(frame.get("ticker", ""))].append(frame)
    rows: list[dict[str, Any]] = []
    for ticker, group in grouped.items():
        materiality = sum(int(row["materiality_score"]) for row in group)
        relevance = sum(int(row["value_chain_relevance_score"]) for row in group)
        event_types = Counter(row["event_type"] for row in group)
        objects = Counter(row["dependency_object"] for row in group if row.get("dependency_object"))
        rows.append(
            {
                "ticker": ticker,
                "company_name": group[0].get("company_name", ""),
                "article_count": len(group),
                "materiality_sum": materiality,
                "value_chain_relevance_sum": relevance,
                "avg_materiality": round(materiality / max(len(group), 1), 3),
                "avg_value_chain_relevance": round(relevance / max(len(group), 1), 3),
                "event_type_mix": json.dumps(dict(event_types.most_common()), sort_keys=True),
                "top_dependency_objects": "; ".join(value for value, _ in objects.most_common(8)),
                "error_count": sum(1 for row in group if row.get("llm_error")),
                "sample_events": " | ".join(str(row.get("event_summary", "")) for row in group[:4] if row.get("event_summary")),
            }
        )
    return sorted(rows, key=lambda row: (-row["materiality_sum"], -row["value_chain_relevance_sum"], row["ticker"]))


def clamp_int(value: Any, lower: int, upper: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = lower
    return min(max(parsed, lower), upper)


def clamp_float(value: Any, lower: float, upper: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = lower
    return round(min(max(parsed, lower), upper), 3)


def normalize_direction(value: Any) -> str:
    direction = str(value or "unknown").lower()
    if direction in {"beneficial", "adverse", "mixed", "neutral", "unknown"}:
        return direction
    return "unknown"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
