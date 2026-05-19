from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


BRANCH_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = BRANCH_DIR / "outputs" / "gdelt_news" / "gdelt_articles.jsonl"

THEME_KEYWORDS = {
    "ai_demand_capex": [
        "ai",
        "artificial intelligence",
        "gpu",
        "accelerator",
        "capex",
        "capital expenditure",
        "h100",
        "blackwell",
    ],
    "data_center_power": [
        "data center",
        "datacenter",
        "power",
        "electricity",
        "grid",
        "nuclear",
        "utility",
        "energy",
    ],
    "semiconductor_supply": [
        "semiconductor",
        "chip",
        "foundry",
        "wafer",
        "packaging",
        "hbm",
        "memory",
        "tsmc",
        "asml",
    ],
    "cloud_platform": ["cloud", "aws", "azure", "google cloud", "oracle cloud"],
    "earnings_guidance": ["earnings", "revenue", "guidance", "quarter", "profit", "forecast", "results"],
    "partnership_deal": ["partner", "partnership", "deal", "agreement", "contract", "collaboration", "alliance"],
    "regulation_geopolitics": ["export", "china", "tariff", "regulation", "sanction", "government", "policy"],
    "market_reaction": ["stock", "shares", "analyst", "price target", "buy", "sell", "upgrade", "downgrade"],
}
HIGH_RELEVANCE_THEMES = {
    "ai_demand_capex",
    "data_center_power",
    "semiconductor_supply",
    "cloud_platform",
    "partnership_deal",
    "regulation_geopolitics",
}
TIER1_DOMAINS = {
    "reuters.com",
    "apnews.com",
    "cnbc.com",
    "bloomberg.com",
    "wsj.com",
    "ft.com",
    "theinformation.com",
    "datacenterdynamics.com",
    "utilitydive.com",
    "semianalysis.com",
}
TIER2_DOMAINS = {
    "finance.yahoo.com",
    "seekingalpha.com",
    "marketwatch.com",
    "barrons.com",
    "fool.com",
    "benzinga.com",
    "investorplace.com",
    "techcrunch.com",
    "theregister.com",
}
LOW_SIGNAL_DOMAINS = {
    "tickerreport.com",
    "dailypolitical.com",
    "defenseworld.net",
    "etfdailynews.com",
    "biztoc.com",
    "americanbankingnews.com",
}


def main() -> int:
    args = parse_args()
    rows = read_jsonl(Path(args.input))
    out_dir = Path(args.output_dir) if args.output_dir else Path(args.input).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    article_rows = [annotate_article(row) for row in rows]
    article_rows = sorted(article_rows, key=lambda row: (-float(row["event_relevance_score"]), row["ticker"], row["title"]))
    company_rows = summarize_by_company(article_rows)
    theme_rows = summarize_by_theme(article_rows)
    mode_rows = summarize_by_query_mode(article_rows)
    payload = {
        "article_count": len(article_rows),
        "company_count": len(company_rows),
        "theme_count": len(theme_rows),
        "top_companies": company_rows[:25],
        "top_themes": theme_rows[:25],
        "query_modes": mode_rows,
    }

    write_jsonl(out_dir / "gdelt_articles_annotated.jsonl", article_rows)
    write_csv(out_dir / "gdelt_company_summary.csv", company_rows)
    write_csv(out_dir / "gdelt_theme_summary.csv", theme_rows)
    write_csv(out_dir / "gdelt_query_mode_summary.csv", mode_rows)
    write_json(out_dir / "gdelt_summary.json", payload)

    print(f"Read {len(article_rows)} articles from {args.input}")
    print(f"Wrote summaries to {out_dir}")
    for row in company_rows[:15]:
        print(
            row["ticker"],
            "articles=", row["article_count"],
            "event_score=", row["event_relevance_score"],
            "dominant=", row["dominant_theme"],
        )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize local GDELT news overlay records.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def annotate_article(row: dict[str, Any]) -> dict[str, Any]:
    title = str(row.get("title", ""))
    domain = str(row.get("domain", ""))
    themes = classify_title(title)
    primary = themes[0] if themes else "other"
    tier = source_tier(domain)
    query_modes = split_semicolon(str(row.get("query_modes") or row.get("query_mode", "")))
    sec_objects = split_semicolon(str(row.get("sec_objects") or row.get("sec_object", "")))
    score = event_relevance_score(primary, themes, tier, query_modes, sec_objects, title)
    return {
        **row,
        "themes": ";".join(themes),
        "primary_theme": primary,
        "source_tier": tier,
        "query_modes": ";".join(query_modes),
        "sec_objects": ";".join(sec_objects),
        "event_relevance_score": score,
        "headline_quality_flag": headline_quality_flag(title, domain),
    }


def classify_title(title: str) -> list[str]:
    lower = title.lower()
    hits: list[str] = []
    for theme, keywords in THEME_KEYWORDS.items():
        if any(keyword in lower for keyword in keywords):
            hits.append(theme)
    return hits or ["other"]


def source_tier(domain: str) -> str:
    normalized = domain.lower().removeprefix("www.")
    if normalized in TIER1_DOMAINS:
        return "tier1"
    if normalized in TIER2_DOMAINS:
        return "tier2"
    if normalized in LOW_SIGNAL_DOMAINS:
        return "low_signal"
    return "other"


def event_relevance_score(
    primary_theme: str,
    themes: list[str],
    tier: str,
    query_modes: list[str],
    sec_objects: list[str],
    title: str,
) -> float:
    score = 1.0
    if primary_theme in HIGH_RELEVANCE_THEMES:
        score += 2.5
    if "market_reaction" in themes and len(set(themes) & HIGH_RELEVANCE_THEMES) == 0:
        score -= 0.8
    if tier == "tier1":
        score += 1.5
    elif tier == "tier2":
        score += 0.7
    elif tier == "low_signal":
        score -= 0.7
    if "sec_object" in query_modes:
        score += 1.25
    if "value_chain" in query_modes:
        score += 0.5
    if sec_objects:
        score += min(1.0, 0.35 * len(sec_objects))
    if headline_quality_flag(title, "") != "ok":
        score -= 0.5
    return round(max(score, 0.0), 3)


def headline_quality_flag(title: str, domain: str) -> str:
    lower = title.lower()
    if not title.strip():
        return "empty"
    if "purchases" in lower and "shares" in lower:
        return "holdings_filing"
    if "q1 2026 earnings transcript" in lower or "earnings transcript" in lower:
        return "transcript_syndication"
    if source_tier(domain) == "low_signal":
        return "low_signal_domain"
    return "ok"


def summarize_by_company(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("ticker", ""))].append(row)
    output: list[dict[str, Any]] = []
    for ticker, group in grouped.items():
        themes = Counter(row["primary_theme"] for row in group)
        domains = Counter(str(row.get("domain", "")) for row in group if row.get("domain"))
        query_modes = Counter(mode for row in group for mode in split_semicolon(str(row.get("query_modes", ""))))
        quality_flags = Counter(row.get("headline_quality_flag", "") for row in group)
        event_score = round(sum(float(row.get("event_relevance_score") or 0) for row in group), 3)
        high_quality_count = sum(1 for row in group if row.get("source_tier") in {"tier1", "tier2"})
        output.append(
            {
                "ticker": ticker,
                "company_name": group[0].get("company_name", ""),
                "article_count": len(group),
                "unique_domain_count": len(domains),
                "high_quality_article_count": high_quality_count,
                "event_relevance_score": event_score,
                "avg_event_relevance": round(event_score / max(len(group), 1), 3),
                "dominant_theme": themes.most_common(1)[0][0] if themes else "other",
                "theme_mix": json.dumps(dict(themes.most_common()), sort_keys=True),
                "query_mode_mix": json.dumps(dict(query_modes.most_common()), sort_keys=True),
                "quality_flag_mix": json.dumps(dict(quality_flags.most_common()), sort_keys=True),
                "top_domains": "; ".join(domain for domain, _ in domains.most_common(8)),
                "top_titles": " | ".join(str(row.get("title", "")) for row in group[:5]),
            }
        )
    return sorted(output, key=lambda row: (-float(row["event_relevance_score"]), row["ticker"]))


def summarize_by_theme(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["primary_theme"]].append(row)
    output: list[dict[str, Any]] = []
    for theme, group in grouped.items():
        tickers = Counter(str(row.get("ticker", "")) for row in group)
        output.append(
            {
                "theme": theme,
                "article_count": len(group),
                "company_count": len(tickers),
                "event_relevance_score": round(sum(float(row.get("event_relevance_score") or 0) for row in group), 3),
                "top_tickers": "; ".join(ticker for ticker, _ in tickers.most_common(10)),
                "sample_titles": " | ".join(str(row.get("title", "")) for row in group[:6]),
            }
        )
    return sorted(output, key=lambda row: (-float(row["event_relevance_score"]), row["theme"]))


def summarize_by_query_mode(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for mode in split_semicolon(str(row.get("query_modes", ""))):
            grouped[mode].append(row)
    output: list[dict[str, Any]] = []
    for mode, group in grouped.items():
        output.append(
            {
                "query_mode": mode,
                "article_count": len(group),
                "event_relevance_score": round(sum(float(row.get("event_relevance_score") or 0) for row in group), 3),
                "avg_event_relevance": round(
                    sum(float(row.get("event_relevance_score") or 0) for row in group) / max(len(group), 1),
                    3,
                ),
                "top_tickers": "; ".join(ticker for ticker, _ in Counter(str(row.get("ticker", "")) for row in group).most_common(10)),
            }
        )
    return sorted(output, key=lambda row: (-float(row["event_relevance_score"]), row["query_mode"]))


def split_semicolon(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


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


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
