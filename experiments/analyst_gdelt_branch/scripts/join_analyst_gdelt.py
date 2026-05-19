from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any


BRANCH_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SCORECARD = BRANCH_DIR / "outputs" / "analyst_lens" / "industry-sec-exhibits-v3" / "company_scorecard.csv"
DEFAULT_GDELT = BRANCH_DIR / "outputs" / "gdelt_news" / "gdelt_company_summary.csv"
DEFAULT_OUTPUT = BRANCH_DIR / "outputs" / "combined" / "company_thesis_monitor.csv"


def main() -> int:
    args = parse_args()
    scorecard = read_csv(Path(args.scorecard))
    news = read_csv(Path(args.gdelt_summary))
    news_by_ticker = {row["ticker"]: row for row in news}
    rows: list[dict[str, Any]] = []
    for row in scorecard:
        ticker = row["ticker"]
        news_row = news_by_ticker.get(ticker, {})
        article_count = int(news_row.get("article_count") or 0)
        dependency_intensity = float(row.get("dependency_intensity") or 0)
        chokepoint = int(row.get("chokepoint_exposure") or 0)
        news_attention_score = article_count * (1 + min(dependency_intensity, 12) / 12)
        thesis_pressure_score = round(news_attention_score + 0.1 * chokepoint, 3)
        rows.append(
            {
                "ticker": ticker,
                "company_name": row.get("company_name", ""),
                "role": row.get("role", ""),
                "analyst_bucket": row.get("analyst_bucket", ""),
                "dependency_intensity": row.get("dependency_intensity", ""),
                "chokepoint_exposure": row.get("chokepoint_exposure", ""),
                "fragility_ratio": row.get("fragility_ratio", ""),
                "customer_concentration_count": row.get("customer_concentration_count", ""),
                "power_infra_relation_count": row.get("power_infra_relation_count", ""),
                "cloud_data_relation_count": row.get("cloud_data_relation_count", ""),
                "gdelt_article_count": article_count,
                "gdelt_dominant_theme": news_row.get("dominant_theme", ""),
                "gdelt_theme_mix": news_row.get("theme_mix", ""),
                "gdelt_top_domains": news_row.get("top_domains", ""),
                "thesis_pressure_score": thesis_pressure_score,
                "monitor_read": monitor_read(row, news_row, thesis_pressure_score),
                "top_dependency_objects": row.get("top_dependency_objects", ""),
                "gdelt_top_titles": news_row.get("top_titles", ""),
            }
        )
    rows.sort(key=lambda item: (-float(item["thesis_pressure_score"]), item["ticker"]))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(output, rows)
    print(f"Wrote {len(rows)} combined thesis rows to {output}")
    for row in rows[:12]:
        print(row["ticker"], row["thesis_pressure_score"], row["monitor_read"])
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Join SEC analyst scorecard with GDELT news summary.")
    parser.add_argument("--scorecard", default=str(DEFAULT_SCORECARD))
    parser.add_argument("--gdelt-summary", default=str(DEFAULT_GDELT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args()


def monitor_read(score_row: dict[str, str], news_row: dict[str, str], pressure: float) -> str:
    bucket = score_row.get("analyst_bucket", "")
    theme = news_row.get("dominant_theme", "")
    article_count = int(news_row.get("article_count") or 0)
    if not news_row:
        return f"No GDELT smoke coverage; SEC bucket remains {bucket}."
    if pressure >= 70:
        return f"High-priority monitor: {bucket} with {article_count} recent articles, led by {theme}."
    if theme in {"data_center_power", "semiconductor_supply"}:
        return f"Relevant news overlay: {theme} aligns with SEC-derived {bucket}."
    if theme == "market_reaction":
        return f"Mostly market-news overlay; use SEC evidence before treating as operating signal."
    return f"Moderate monitor: {bucket} with news theme {theme}."


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())

