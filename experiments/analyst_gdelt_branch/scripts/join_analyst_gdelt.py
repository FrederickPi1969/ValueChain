from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any


BRANCH_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SCORECARD = BRANCH_DIR / "outputs" / "analyst_lens" / "industry-sec-exhibits-v3" / "company_scorecard.csv"
DEFAULT_GDELT = BRANCH_DIR / "outputs" / "gdelt_news_large" / "gdelt_company_summary.csv"
DEFAULT_LLM_SUMMARY = BRANCH_DIR / "outputs" / "gdelt_news_large" / "event_frames_llm.summary.csv"
DEFAULT_OUTPUT = BRANCH_DIR / "outputs" / "combined" / "company_thesis_monitor_large.csv"

STRUCTURAL_FIELDS = [
    "investment_relevance_pct",
    "dependency_risk_pct",
    "operating_dependency_pct",
    "chokepoint_exposure_pct",
    "customer_concentration_pct",
    "capex_beneficiary_pct",
    "fragility_pct",
    "evidence_quality_pct",
]
HIGH_VALUE_NEWS_THEMES = {
    "ai_demand_capex",
    "data_center_power",
    "semiconductor_supply",
    "cloud_platform",
    "partnership_deal",
    "regulation_geopolitics",
}
SEC_THEME_AFFINITY = {
    "ai_demand_capex": {"capex_beneficiary_pct", "operating_dependency_pct", "investment_relevance_pct"},
    "data_center_power": {"capex_beneficiary_pct", "dependency_risk_pct", "operating_dependency_pct"},
    "semiconductor_supply": {"dependency_risk_pct", "chokepoint_exposure_pct", "operating_dependency_pct"},
    "cloud_platform": {"cloud_data_relation_count", "operating_dependency_pct"},
    "partnership_deal": {"strategic_count", "operating_dependency_pct"},
    "regulation_geopolitics": {"fragility_pct", "dependency_risk_pct"},
    "market_reaction": {"investment_relevance_pct"},
}


def main() -> int:
    args = parse_args()
    scorecard = read_csv(Path(args.scorecard))
    news = read_csv(Path(args.gdelt_summary))
    news_by_ticker = {row["ticker"]: row for row in news}
    llm_summary_path = Path(args.llm_summary)
    llm_rows = read_csv(llm_summary_path) if llm_summary_path.exists() else []
    llm_by_ticker = {row["ticker"]: row for row in llm_rows}
    rows: list[dict[str, Any]] = []

    for sec_row in scorecard:
        ticker = sec_row["ticker"]
        news_row = news_by_ticker.get(ticker, {})
        llm_row = llm_by_ticker.get(ticker, {})
        sec_factor_score = structural_score(sec_row)
        news_event_score = news_score(news_row)
        alignment_score = value_chain_alignment(sec_row, news_row)
        quality_score = coverage_quality(news_row)
        llm_event_score = llm_materiality_score(llm_row)
        raw_priority = combined_priority(
            sec_factor_score=sec_factor_score,
            news_event_score=news_event_score,
            alignment_score=alignment_score,
            quality_score=quality_score,
            llm_event_score=llm_event_score,
            has_llm=bool(llm_row),
        )
        monitor_priority = apply_theme_caps(raw_priority, news_row)
        rows.append(
            {
                "ticker": ticker,
                "company_name": sec_row.get("company_name", ""),
                "role": sec_row.get("role", ""),
                "analyst_bucket": sec_row.get("analyst_bucket", ""),
                "monitor_tier": monitor_tier(monitor_priority, news_row),
                "monitor_priority_score": monitor_priority,
                "sec_factor_score": sec_factor_score,
                "news_event_score": news_event_score,
                "value_chain_alignment_score": alignment_score,
                "coverage_quality_score": quality_score,
                "llm_event_score": llm_event_score,
                "leading_sec_factor": leading_sec_factor(sec_row),
                "dominant_news_theme": news_row.get("dominant_theme", ""),
                "article_count": to_int(news_row.get("article_count")),
                "high_quality_article_count": to_int(news_row.get("high_quality_article_count")),
                "avg_event_relevance": to_float(news_row.get("avg_event_relevance")),
                "event_relevance_score": to_float(news_row.get("event_relevance_score")),
                "query_mode_mix": news_row.get("query_mode_mix", ""),
                "quality_flag_mix": news_row.get("quality_flag_mix", ""),
                "theme_mix": news_row.get("theme_mix", ""),
                "top_domains": news_row.get("top_domains", ""),
                "llm_article_count": to_int(llm_row.get("article_count")),
                "llm_avg_materiality": to_float(llm_row.get("avg_materiality")),
                "llm_avg_value_chain_relevance": to_float(llm_row.get("avg_value_chain_relevance")),
                "llm_event_type_mix": llm_row.get("event_type_mix", ""),
                "llm_top_dependency_objects": llm_row.get("top_dependency_objects", ""),
                "llm_sample_events": llm_row.get("sample_events", ""),
                "investment_relevance_pct": sec_row.get("investment_relevance_pct", ""),
                "dependency_risk_pct": sec_row.get("dependency_risk_pct", ""),
                "operating_dependency_pct": sec_row.get("operating_dependency_pct", ""),
                "chokepoint_exposure_pct": sec_row.get("chokepoint_exposure_pct", ""),
                "customer_concentration_pct": sec_row.get("customer_concentration_pct", ""),
                "capex_beneficiary_pct": sec_row.get("capex_beneficiary_pct", ""),
                "fragility_pct": sec_row.get("fragility_pct", ""),
                "evidence_quality_pct": sec_row.get("evidence_quality_pct", ""),
                "top_dependency_objects": sec_row.get("top_dependency_objects", ""),
                "top_named_counterparties": sec_row.get("top_named_counterparties", ""),
                "top_geographies": sec_row.get("top_geographies", ""),
                "analyst_read": analyst_read(sec_row, news_row, monitor_priority, alignment_score),
                "next_drilldown": next_drilldown(sec_row, news_row),
                "top_news_titles": news_row.get("top_titles", ""),
            }
        )

    rows.sort(key=lambda item: (-float(item["monitor_priority_score"]), item["ticker"]))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(output, rows)
    write_json(output.with_suffix(".summary.json"), summary_payload(rows))
    print(f"Wrote {len(rows)} combined thesis rows to {output}")
    for row in rows[:15]:
        print(row["ticker"], row["monitor_priority_score"], row["monitor_tier"], row["analyst_read"])
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Join SEC analyst scorecard with GDELT news summary.")
    parser.add_argument("--scorecard", default=str(DEFAULT_SCORECARD))
    parser.add_argument("--gdelt-summary", default=str(DEFAULT_GDELT))
    parser.add_argument("--llm-summary", default=str(DEFAULT_LLM_SUMMARY))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args()


def combined_priority(
    sec_factor_score: float,
    news_event_score: float,
    alignment_score: float,
    quality_score: float,
    llm_event_score: float,
    has_llm: bool,
) -> float:
    if has_llm:
        score = (
            0.52 * sec_factor_score
            + 0.17 * news_event_score
            + 0.11 * alignment_score
            + 0.06 * quality_score
            + 0.14 * llm_event_score
        )
    else:
        score = (
            0.58 * sec_factor_score
            + 0.22 * news_event_score
            + 0.13 * alignment_score
            + 0.07 * quality_score
        )
    return round(min(max(score, 0.0), 100.0), 3)


def structural_score(row: dict[str, str]) -> float:
    investment = to_float(row.get("investment_relevance_pct"))
    dependency = to_float(row.get("dependency_risk_pct"))
    operating = to_float(row.get("operating_dependency_pct"))
    chokepoint = to_float(row.get("chokepoint_exposure_pct"))
    capex = to_float(row.get("capex_beneficiary_pct"))
    concentration = to_float(row.get("customer_concentration_pct"))
    fragility = to_float(row.get("fragility_pct"))
    evidence = to_float(row.get("evidence_quality_pct"))
    score = (
        0.26 * investment
        + 0.18 * dependency
        + 0.16 * operating
        + 0.12 * chokepoint
        + 0.12 * capex
        + 0.07 * concentration
        + 0.05 * fragility
        + 0.04 * evidence
    )
    return round(min(max(score, 0.0), 100.0), 3)


def news_score(row: dict[str, str]) -> float:
    if not row:
        return 0.0
    article_count = to_int(row.get("article_count"))
    event_relevance = to_float(row.get("event_relevance_score"))
    avg_relevance = to_float(row.get("avg_event_relevance"))
    high_quality = to_int(row.get("high_quality_article_count"))
    breadth = min(35.0, math.log1p(article_count) * 9.0)
    relevance = min(45.0, event_relevance * 0.85)
    per_article_quality = min(12.0, avg_relevance * 2.2)
    source_quality = min(8.0, high_quality * 1.2)
    return round(min(breadth + relevance + per_article_quality + source_quality, 100.0), 3)


def llm_materiality_score(row: dict[str, str]) -> float:
    if not row:
        return 0.0
    avg_materiality = to_float(row.get("avg_materiality"))
    avg_relevance = to_float(row.get("avg_value_chain_relevance"))
    article_count = to_int(row.get("article_count"))
    event_mix = parse_json_counter(row.get("event_type_mix", ""))
    noise_count = event_mix.get("market_price_action", 0) + event_mix.get("low_signal_noise", 0)
    structural_event_count = sum(
        count
        for event_type, count in event_mix.items()
        if event_type
        in {
            "demand_capex",
            "datacenter_power",
            "semiconductor_supply",
            "cloud_platform",
            "manufacturing_capacity",
            "partnership_contract",
            "regulation_geopolitics",
            "earnings_guidance",
            "customer_concentration",
        }
    )
    score = (
        avg_materiality / 3.0 * 42.0
        + avg_relevance / 3.0 * 38.0
        + min(12.0, article_count * 1.5)
        + min(8.0, structural_event_count * 1.5)
        - min(20.0, noise_count * 2.2)
    )
    return round(min(max(score, 0.0), 100.0), 3)


def value_chain_alignment(sec_row: dict[str, str], news_row: dict[str, str]) -> float:
    if not news_row:
        return 0.0
    theme = news_row.get("dominant_theme", "")
    affinity_fields = SEC_THEME_AFFINITY.get(theme, set())
    if not affinity_fields:
        return 25.0
    field_scores: list[float] = []
    for field in affinity_fields:
        if field.endswith("_pct"):
            field_scores.append(to_float(sec_row.get(field)))
        else:
            field_scores.append(min(100.0, to_float(sec_row.get(field)) * 12.0))
    query_mix = parse_json_counter(news_row.get("query_mode_mix", ""))
    mode_bonus = 0.0
    if query_mix.get("value_chain"):
        mode_bonus += 8.0
    if query_mix.get("sec_object"):
        mode_bonus += 12.0
    if theme in HIGH_VALUE_NEWS_THEMES:
        mode_bonus += 5.0
    return round(min((sum(field_scores) / max(len(field_scores), 1)) + mode_bonus, 100.0), 3)


def coverage_quality(row: dict[str, str]) -> float:
    if not row:
        return 0.0
    article_count = to_int(row.get("article_count"))
    high_quality = to_int(row.get("high_quality_article_count"))
    domain_count = to_int(row.get("unique_domain_count"))
    quality_flags = parse_json_counter(row.get("quality_flag_mix", ""))
    noisy = quality_flags.get("holdings_filing", 0) + quality_flags.get("low_signal_domain", 0)
    base = min(45.0, high_quality * 4.5) + min(30.0, domain_count * 3.0) + min(25.0, article_count * 0.8)
    penalty = min(35.0, noisy * 2.5)
    return round(min(max(base - penalty, 0.0), 100.0), 3)


def monitor_tier(score: float, news_row: dict[str, str]) -> str:
    if news_row.get("dominant_theme") == "market_reaction":
        if score >= 62:
            return "sec_thesis_market_noise"
        return "market_noise_watch"
    if score >= 78:
        return "high_priority"
    if score >= 62:
        return "active_watch"
    if score >= 46:
        return "background_watch"
    if not news_row:
        return "sec_only_no_recent_news"
    return "low_signal"


def leading_sec_factor(row: dict[str, str]) -> str:
    scores = [(field, to_float(row.get(field))) for field in STRUCTURAL_FIELDS]
    field, _ = max(scores, key=lambda item: item[1])
    return field.removesuffix("_pct")


def analyst_read(
    sec_row: dict[str, str],
    news_row: dict[str, str],
    priority: float,
    alignment_score: float,
) -> str:
    bucket = sec_row.get("analyst_bucket", "")
    leading = leading_sec_factor(sec_row).replace("_", " ")
    theme = news_row.get("dominant_theme", "") or "no recent GDELT signal"
    article_count = to_int(news_row.get("article_count"))
    if not news_row:
        return f"SEC-only {bucket}; leading factor is {leading}, but no matching GDELT coverage in this window."
    if theme == "market_reaction":
        return f"SEC-derived {bucket} remains the operating thesis; {article_count} recent articles are mostly market reaction."
    if priority >= 78 and alignment_score >= 70:
        return f"High-conviction monitor: SEC {bucket} is reinforced by {article_count} news items around {theme}."
    if priority >= 62:
        return f"Active watch: SEC leading factor is {leading}; recent coverage is led by {theme}."
    return f"Background watch: {bucket} with {theme} coverage, but alignment is not yet strong."


def apply_theme_caps(score: float, news_row: dict[str, str]) -> float:
    theme = news_row.get("dominant_theme", "")
    if theme == "market_reaction":
        return round(min(score, 76.0), 3)
    if theme == "other" and to_float(news_row.get("avg_event_relevance")) < 3.0:
        return round(min(score, 68.0), 3)
    return score


def next_drilldown(sec_row: dict[str, str], news_row: dict[str, str]) -> str:
    theme = news_row.get("dominant_theme", "")
    leading = leading_sec_factor(sec_row)
    if theme == "data_center_power" or leading == "capex_beneficiary":
        return "Inspect power, utility, data center, and capacity passages; separate committed contracts from risk language."
    if theme == "semiconductor_supply" or leading in {"dependency_risk", "chokepoint_exposure"}:
        return "Inspect foundry, packaging, sole-source supplier, export-control, and named counterparty evidence."
    if theme == "cloud_platform":
        return "Inspect cloud-hosting and customer concentration edges; distinguish platform dependency from customer demand."
    if theme == "market_reaction":
        return "Filter out stock-rating headlines and validate operating facts against SEC evidence."
    return "Review top SEC evidence rows and named counterparties before promoting to thesis."


def summary_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tiers: dict[str, int] = {}
    themes: dict[str, int] = {}
    for row in rows:
        tiers[row["monitor_tier"]] = tiers.get(row["monitor_tier"], 0) + 1
        theme = str(row.get("dominant_news_theme") or "none")
        themes[theme] = themes.get(theme, 0) + 1
    return {
        "company_count": len(rows),
        "tier_counts": tiers,
        "theme_counts": themes,
        "top_monitors": rows[:20],
    }


def parse_json_counter(value: str) -> dict[str, int]:
    if not value:
        return {}
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return {str(key): int(count) for key, count in raw.items()}


def to_float(value: Any) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def to_int(value: Any) -> int:
    if value in {None, ""}:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


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
