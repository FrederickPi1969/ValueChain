from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_WEIGHTS = {
    "market_cap": 0.35,
    "liquidity": 0.25,
    "etf": 0.15,
    "index": 0.10,
    "value_chain": 0.15,
}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "critical"}


def _percentile_map(values: dict[int, float]) -> dict[int, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0}
    output: dict[int, float] = {}
    start = 0
    denominator = len(ordered) - 1
    while start < len(ordered):
        end = start
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[start][1]:
            end += 1
        percentile = ((start + end) / 2) / denominator
        for position in range(start, end + 1):
            output[ordered[position][0]] = percentile
        start = end + 1
    return output


def _group_percentiles(
    rows: list[dict[str, Any]],
    *,
    value_column: str,
    group_columns: tuple[str, ...] = ("jurisdiction",),
) -> dict[int, float]:
    groups: dict[tuple[str, ...], dict[int, float]] = defaultdict(dict)
    for index, row in enumerate(rows):
        value = _number(row.get(value_column))
        if value is None:
            continue
        key = tuple(str(row.get(column) or "").strip() for column in group_columns)
        groups[key][index] = value
    output: dict[int, float] = {}
    for members in groups.values():
        output.update(_percentile_map(members))
    return output


def assign_tiers(
    rows: list[dict[str, Any]],
    *,
    weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    policy = dict(DEFAULT_WEIGHTS)
    if weights:
        policy.update(weights)
    if abs(sum(policy.values()) - 1.0) > 1e-9:
        raise ValueError("Tier weights must sum to 1.0")

    cap_pct = _group_percentiles(rows, value_column="market_cap_usd")
    liquidity_pct = _group_percentiles(rows, value_column="median_daily_value_usd")
    etf_pct = _group_percentiles(rows, value_column="etf_core_weight", group_columns=("global",))
    index_pct = _group_percentiles(
        rows, value_column="index_membership_count", group_columns=("global",)
    )

    output: list[dict[str, Any]] = []
    for index, original in enumerate(rows):
        row = dict(original)
        components = {
            "market_cap": cap_pct.get(index, 0.0),
            "liquidity": liquidity_pct.get(index, 0.0),
            "etf": etf_pct.get(index, 0.0),
            "index": index_pct.get(index, 0.0),
            "value_chain": min(max(_number(row.get("value_chain_score")) or 0.0, 0.0), 1.0),
        }
        score = sum(components[name] * policy[name] for name in policy)
        critical = _truthy(row.get("critical_supply_chain"))
        if critical:
            score = min(score + 0.12, 1.0)

        reasons: list[str] = []
        if components["market_cap"] >= 0.9:
            reasons.append("top-decile market capitalization in jurisdiction")
        if components["liquidity"] >= 0.9:
            reasons.append("top-decile trading liquidity in jurisdiction")
        if components["etf"] >= 0.8:
            reasons.append("material ETF core exposure")
        if components["value_chain"] >= 0.7:
            reasons.append("high AI/value-chain score")
        if critical:
            reasons.append("critical supply-chain override")

        if score >= 0.70 or (
            components["market_cap"] >= 0.9 and components["etf"] >= 0.6
        ):
            tier = "Tier 1"
        elif score >= 0.40 or critical or components["value_chain"] >= 0.7:
            tier = "Tier 2"
        else:
            tier = "Tier 3"

        row.update(
            {
                "tier": tier,
                "tier_score": f"{score:.6f}",
                "market_cap_percentile": f"{components['market_cap']:.6f}",
                "liquidity_percentile": f"{components['liquidity']:.6f}",
                "etf_percentile": f"{components['etf']:.6f}",
                "index_percentile": f"{components['index']:.6f}",
                "tier_reasons": "; ".join(reasons) or "denominator retention",
            }
        )
        output.append(row)
    return output


def read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def tier_csv(input_path: Path, output_path: Path) -> dict[str, Any]:
    rows = read_csv(input_path)
    tiered = assign_tiers(rows)
    write_csv(output_path, tiered)
    counts: dict[str, int] = defaultdict(int)
    for row in tiered:
        counts[str(row["tier"])] += 1
    return {
        "input_path": str(input_path),
        "output_path": str(output_path),
        "input_rows": len(rows),
        "output_rows": len(tiered),
        "all_denominator_rows_preserved": len(rows) == len(tiered),
        "tier_counts": dict(sorted(counts.items())),
        "weights": DEFAULT_WEIGHTS,
    }
