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
    ],
    "data_center_power": [
        "data center",
        "datacenter",
        "power",
        "electricity",
        "grid",
        "nuclear",
        "utility",
    ],
    "semiconductor_supply": [
        "semiconductor",
        "chip",
        "foundry",
        "wafer",
        "packaging",
        "hbm",
        "memory",
    ],
    "cloud_platform": [
        "cloud",
        "aws",
        "azure",
        "google cloud",
        "oracle cloud",
    ],
    "earnings_guidance": [
        "earnings",
        "revenue",
        "guidance",
        "quarter",
        "profit",
        "forecast",
    ],
    "partnership_deal": [
        "partner",
        "partnership",
        "deal",
        "agreement",
        "contract",
        "collaboration",
    ],
    "regulation_geopolitics": [
        "export",
        "china",
        "tariff",
        "regulation",
        "sanction",
        "government",
    ],
    "market_reaction": [
        "stock",
        "shares",
        "analyst",
        "price target",
        "buy",
        "sell",
        "upgrade",
        "downgrade",
    ],
}


def main() -> int:
    args = parse_args()
    rows = read_jsonl(Path(args.input))
    out_dir = Path(args.output_dir) if args.output_dir else Path(args.input).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    article_rows = [annotate_article(row) for row in rows]
    company_rows = summarize_by_company(article_rows)
    theme_rows = summarize_by_theme(article_rows)
    payload = {
        "article_count": len(article_rows),
        "company_count": len(company_rows),
        "theme_count": len(theme_rows),
        "top_companies": company_rows[:20],
        "top_themes": theme_rows[:20],
    }

    write_csv(out_dir / "gdelt_company_summary.csv", company_rows)
    write_csv(out_dir / "gdelt_theme_summary.csv", theme_rows)
    write_json(out_dir / "gdelt_summary.json", payload)

    print(f"Read {len(article_rows)} articles from {args.input}")
    print(f"Wrote summaries to {out_dir}")
    for row in company_rows[:10]:
        print(row["ticker"], row["article_count"], row["dominant_theme"], row["top_titles"])
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize local GDELT news overlay records.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def annotate_article(row: dict[str, Any]) -> dict[str, Any]:
    title = str(row.get("title", ""))
    themes = classify_title(title)
    return {
        **row,
        "themes": themes,
        "primary_theme": themes[0] if themes else "other",
    }


def classify_title(title: str) -> list[str]:
    lower = title.lower()
    hits: list[str] = []
    for theme, keywords in THEME_KEYWORDS.items():
        if any(keyword in lower for keyword in keywords):
            hits.append(theme)
    return hits or ["other"]


def summarize_by_company(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("ticker", ""))].append(row)
    output: list[dict[str, Any]] = []
    for ticker, group in grouped.items():
        themes = Counter(row["primary_theme"] for row in group)
        domains = Counter(str(row.get("domain", "")) for row in group if row.get("domain"))
        output.append(
            {
                "ticker": ticker,
                "company_name": group[0].get("company_name", ""),
                "article_count": len(group),
                "unique_domain_count": len(domains),
                "dominant_theme": themes.most_common(1)[0][0] if themes else "other",
                "theme_mix": json.dumps(dict(themes.most_common()), sort_keys=True),
                "top_domains": "; ".join(domain for domain, _ in domains.most_common(6)),
                "top_titles": " | ".join(str(row.get("title", "")) for row in group[:4]),
            }
        )
    return sorted(output, key=lambda row: (-int(row["article_count"]), row["ticker"]))


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
                "top_tickers": "; ".join(ticker for ticker, _ in tickers.most_common(10)),
                "sample_titles": " | ".join(str(row.get("title", "")) for row in group[:5]),
            }
        )
    return sorted(output, key=lambda row: (-int(row["article_count"]), row["theme"]))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


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

