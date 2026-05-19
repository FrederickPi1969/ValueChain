from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[3]
BRANCH_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = BRANCH_DIR / "outputs" / "analyst_lens"

SUPPLIER_RELATIONS = {
    "supplier_dependency",
    "manufacturing_dependency",
    "foundry_dependency",
    "packaging_or_assembly_dependency",
    "licensing_dependency",
}
CLOUD_DATA_RELATIONS = {
    "cloud_or_hosting_dependency",
    "data_center_dependency",
    "network_or_interconnection_dependency",
}
POWER_INFRA_RELATIONS = {
    "power_or_utility_dependency",
    "facility_or_geographic_exposure",
}
CUSTOMER_RELATIONS = {"customer_dependency", "concentration_risk"}
CAPEX_BENEFICIARY_ROLE_TERMS = {
    "power",
    "grid",
    "cooling",
    "construction",
    "data_center",
    "optical",
    "server",
    "semicap",
    "foundry",
    "memory",
}
GEOGRAPHY_OBJECTS = {
    "asia",
    "australia",
    "canada",
    "china",
    "europe",
    "hong kong",
    "india",
    "ireland",
    "israel",
    "japan",
    "malaysia",
    "netherlands",
    "russia",
    "singapore",
    "south korea",
    "taiwan",
    "united states",
}
NOISY_OBJECT_PREFIXES = ("contents ", "notes ", "item ", "part ", "table ")
NOISY_OBJECTS = {
    "business overview",
    "entity name",
    "name of subsidiary",
    "risk factors",
}
MODALITY_WEIGHTS = {
    "current_fact": 1.0,
    "strategic": 0.9,
    "historical_fact": 0.55,
    "forward_looking": 0.45,
    "risk_hypothetical": 0.3,
}
RELATION_WEIGHTS = {
    "foundry_dependency": 1.3,
    "manufacturing_dependency": 1.15,
    "packaging_or_assembly_dependency": 1.1,
    "supplier_dependency": 1.0,
    "cloud_or_hosting_dependency": 0.95,
    "data_center_dependency": 1.05,
    "power_or_utility_dependency": 1.15,
    "network_or_interconnection_dependency": 0.85,
    "customer_dependency": 1.1,
    "concentration_risk": 1.0,
    "distribution_or_channel_dependency": 0.75,
    "licensing_dependency": 0.85,
    "facility_or_geographic_exposure": 0.65,
    "strategic_partner": 0.7,
    "co_investment": 0.75,
    "subsidiary_or_control": 0.15,
}


def main() -> int:
    args = parse_args()
    run_dir = ROOT / "data" / "processed" / "runs" / args.run_id
    if not run_dir.exists():
        raise SystemExit(f"Run artifacts not found: {run_dir}")

    out_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    companies = read_csv(run_dir / "company_universe_resolved.csv")
    evidence = read_jsonl(run_dir / "relation_evidence.jsonl")
    edges = read_csv(run_dir / "graph_edges.csv")
    bottlenecks = read_csv(run_dir / "bottleneck_candidates.csv")

    company_rows = build_company_scorecard(companies, evidence, edges, bottlenecks)
    bottleneck_rows = build_bottleneck_thesis(bottlenecks, evidence)
    factor_rows = build_factor_leaderboard(company_rows)
    summary = {
        "run_id": args.run_id,
        "company_count": len(companies),
        "evidence_count": len(evidence),
        "edge_count": len(edges),
        "bottleneck_count": len(bottlenecks),
        "factor_definitions": factor_definitions(),
        "top_company_rows": company_rows[:15],
        "top_bottleneck_rows": bottleneck_rows[:15],
        "factor_leaders": factor_rows[:25],
    }

    write_csv(out_dir / "company_scorecard.csv", company_rows)
    write_csv(out_dir / "bottleneck_thesis.csv", bottleneck_rows)
    write_csv(out_dir / "factor_leaderboard.csv", factor_rows)
    write_json(out_dir / "analyst_summary.json", summary)
    write_markdown_report(out_dir / "analyst_report.md", summary)

    print(f"Wrote analyst lens outputs to {out_dir}")
    print(f"companies={len(company_rows)} evidence={len(evidence)} edges={len(edges)}")
    print("Top Bloomberg-style screens:")
    for row in company_rows[:12]:
        print(
            row["ticker"],
            row["analyst_bucket"],
            "investment_relevance_pct=",
            row["investment_relevance_pct"],
            "thesis=",
            row["analyst_thesis"][:120],
        )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build local analyst-style screens from run artifacts.")
    parser.add_argument("--run-id", default="industry-sec-exhibits-v3")
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def build_company_scorecard(
    companies: list[dict[str, str]],
    evidence: list[dict[str, Any]],
    edges: list[dict[str, str]],
    bottlenecks: list[dict[str, str]],
) -> list[dict[str, Any]]:
    company_by_name = {row["company_name"]: row for row in companies}
    bottleneck_weight = {
        row["object"]: int(row.get("dependent_company_count") or 0)
        for row in bottlenecks
        if row.get("object") and not is_noisy_object(row["object"])
    }
    evidence_by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        evidence_by_subject[str(row.get("subject", ""))].append(row)

    edges_by_subject: dict[str, list[dict[str, str]]] = defaultdict(list)
    for edge in edges:
        edges_by_subject[str(edge.get("subject", ""))].append(edge)

    rows: list[dict[str, Any]] = []
    for company_name, company in company_by_name.items():
        subject_evidence = evidence_by_subject.get(company_name, [])
        subject_edges = edges_by_subject.get(company_name, [])
        features = company_features(company, subject_evidence, subject_edges, bottleneck_weight)
        rows.append(features)

    attach_percentiles(
        rows,
        [
            "dependency_risk_score",
            "operating_dependency_score",
            "chokepoint_exposure_score",
            "customer_concentration_score",
            "capex_beneficiary_score",
            "fragility_score",
            "evidence_quality_score",
            "investment_relevance_score",
        ],
    )
    for row in rows:
        row["analyst_bucket"] = analyst_bucket(row)
        row["analyst_thesis"] = analyst_thesis(row)
    return sorted(
        rows,
        key=lambda row: (
            -float(row["investment_relevance_score"]),
            -float(row["dependency_risk_score"]),
            row["ticker"],
        ),
    )


def company_features(
    company: dict[str, str],
    evidence: list[dict[str, Any]],
    edges: list[dict[str, str]],
    bottleneck_weight: dict[str, int],
) -> dict[str, Any]:
    relation_counts = Counter(row.get("relation_type", "") for row in evidence)
    modality_counts = Counter(row.get("modality", "") for row in evidence)
    object_rows = [(str(row.get("object", "")).strip(), row) for row in evidence if row.get("object")]
    clean_object_rows = [(obj, row) for obj, row in object_rows if not is_noisy_object(obj)]
    objects = {obj for obj, _ in clean_object_rows}
    named_objects = {obj for obj, _ in clean_object_rows if object_quality(obj) == "named_entity"}
    class_objects = {obj for obj, _ in clean_object_rows if object_quality(obj) == "dependency_class"}
    geography_objects = {obj for obj, _ in clean_object_rows if object_quality(obj) == "geography"}

    weighted_operating = weighted_evidence_score(
        evidence,
        allowed_modalities={"current_fact", "strategic", "historical_fact"},
        object_quality_filter={"named_entity", "geography"},
    )
    weighted_risk = weighted_evidence_score(
        evidence,
        allowed_modalities={"risk_hypothetical", "forward_looking"},
        object_quality_filter={"named_entity", "geography", "dependency_class"},
    )
    supplier_count = sum(relation_counts.get(name, 0) for name in SUPPLIER_RELATIONS)
    cloud_data_count = sum(relation_counts.get(name, 0) for name in CLOUD_DATA_RELATIONS)
    power_infra_count = sum(relation_counts.get(name, 0) for name in POWER_INFRA_RELATIONS)
    customer_count = sum(relation_counts.get(name, 0) for name in CUSTOMER_RELATIONS)
    current = modality_counts.get("current_fact", 0)
    risk = modality_counts.get("risk_hypothetical", 0)
    forward = modality_counts.get("forward_looking", 0)
    strategic = modality_counts.get("strategic", 0)
    named_current_count = sum(
        1
        for obj, row in clean_object_rows
        if object_quality(obj) == "named_entity" and row.get("modality") == "current_fact"
    )
    geography_risk_count = sum(
        1
        for obj, row in clean_object_rows
        if object_quality(obj) == "geography" and row.get("modality") in {"risk_hypothetical", "forward_looking"}
    )
    avg_confidence = average(float(row.get("confidence_score") or 0) for row in evidence)
    chokepoint_exposure = sum(bottleneck_weight.get(obj, 0) for obj in objects)

    dependency_risk_score = round(
        0.35 * weighted_operating
        + 0.25 * weighted_risk
        + 0.015 * chokepoint_exposure
        + 0.05 * math.log1p(customer_count)
        + 0.04 * math.log1p(geography_risk_count),
        3,
    )
    operating_dependency_score = round(weighted_operating + 0.15 * named_current_count, 3)
    chokepoint_exposure_score = round(math.log1p(chokepoint_exposure), 3)
    customer_concentration_score = round(math.log1p(customer_count) + 0.3 * relation_counts.get("concentration_risk", 0), 3)
    capex_beneficiary_score = round(capex_role_score(company.get("role", "")) + 0.03 * (power_infra_count + cloud_data_count), 3)
    fragility_score = round((risk + forward) / (current + strategic + 1), 3)
    evidence_quality_score = round(
        100
        * (
            0.55 * safe_ratio(len(named_objects), len(objects))
            + 0.25 * safe_ratio(named_current_count, len(evidence))
            + 0.20 * avg_confidence
        ),
        3,
    )
    investment_relevance_score = round(
        0.32 * dependency_risk_score
        + 0.22 * operating_dependency_score
        + 0.18 * chokepoint_exposure_score
        + 0.14 * customer_concentration_score
        + 0.14 * capex_beneficiary_score,
        3,
    )
    return {
        "ticker": company.get("ticker", ""),
        "company_name": company.get("company_name", ""),
        "role": company.get("role", ""),
        "priority": company.get("priority", ""),
        "evidence_count": len(evidence),
        "edge_count": len(edges),
        "unique_object_count": len(objects),
        "named_object_count": len(named_objects),
        "class_object_count": len(class_objects),
        "geography_object_count": len(geography_objects),
        "named_object_ratio": round(safe_ratio(len(named_objects), len(objects)), 3),
        "avg_confidence": round(avg_confidence, 3),
        "current_fact_count": current,
        "risk_hypothetical_count": risk,
        "forward_looking_count": forward,
        "historical_fact_count": modality_counts.get("historical_fact", 0),
        "strategic_count": strategic,
        "named_current_fact_count": named_current_count,
        "geography_risk_count": geography_risk_count,
        "supplier_relation_count": supplier_count,
        "cloud_data_relation_count": cloud_data_count,
        "power_infra_relation_count": power_infra_count,
        "customer_concentration_count": customer_count,
        "foundry_relation_count": relation_counts.get("foundry_dependency", 0),
        "manufacturing_relation_count": relation_counts.get("manufacturing_dependency", 0),
        "dependency_risk_score": dependency_risk_score,
        "operating_dependency_score": operating_dependency_score,
        "chokepoint_exposure_score": chokepoint_exposure_score,
        "customer_concentration_score": customer_concentration_score,
        "capex_beneficiary_score": capex_beneficiary_score,
        "fragility_score": fragility_score,
        "evidence_quality_score": evidence_quality_score,
        "investment_relevance_score": investment_relevance_score,
        "top_dependency_objects": "; ".join(top_objects(evidence, limit=10)),
        "top_named_counterparties": "; ".join(top_objects(evidence, limit=10, qualities={"named_entity"})),
        "top_geographies": "; ".join(top_objects(evidence, limit=8, qualities={"geography"})),
    }


def build_bottleneck_thesis(
    bottlenecks: list[dict[str, str]],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence_by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        obj = str(row.get("object", "")).strip()
        if obj and not is_noisy_object(obj):
            evidence_by_object[obj].append(row)

    rows: list[dict[str, Any]] = []
    for row in bottlenecks:
        obj = row.get("object", "")
        if is_noisy_object(obj):
            continue
        obj_evidence = evidence_by_object.get(obj, [])
        modality_counts = Counter(item.get("modality", "") for item in obj_evidence)
        relation_counts = Counter(item.get("relation_type", "") for item in obj_evidence)
        dependent_company_count = int(row.get("dependent_company_count") or 0)
        evidence_count = int(row.get("evidence_count") or 0)
        operating_count = (
            modality_counts.get("current_fact", 0)
            + modality_counts.get("strategic", 0)
            + modality_counts.get("historical_fact", 0)
        )
        risk_count = modality_counts.get("risk_hypothetical", 0) + modality_counts.get("forward_looking", 0)
        quality = object_quality(obj)
        rows.append(
            {
                "object": obj,
                "object_quality": quality,
                "theme": bottleneck_theme(obj, relation_counts),
                "dependent_company_count": dependent_company_count,
                "evidence_count": evidence_count,
                "operating_evidence_count": operating_count,
                "risk_evidence_count": risk_count,
                "operating_share": round(safe_ratio(operating_count, operating_count + risk_count), 3),
                "current_fact_count": modality_counts.get("current_fact", 0),
                "risk_hypothetical_count": modality_counts.get("risk_hypothetical", 0),
                "forward_looking_count": modality_counts.get("forward_looking", 0),
                "relation_types": row.get("relation_types", ""),
                "subjects": row.get("subjects", ""),
                "bottleneck_score": round(
                    math.log1p(dependent_company_count) * (1 + math.log1p(evidence_count))
                    * (1.25 if quality == "named_entity" else 0.85 if quality == "geography" else 0.65),
                    3,
                ),
                "analyst_read": bottleneck_read(obj, relation_counts, modality_counts, quality),
            }
        )
    attach_percentiles(rows, ["bottleneck_score"])
    return sorted(
        rows,
        key=lambda row: (-float(row["bottleneck_score"]), -int(row["dependent_company_count"]), row["object"]),
    )


def build_factor_leaderboard(company_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    factors = [
        "dependency_risk_pct",
        "operating_dependency_pct",
        "chokepoint_exposure_pct",
        "customer_concentration_pct",
        "capex_beneficiary_pct",
        "fragility_pct",
        "evidence_quality_pct",
        "investment_relevance_pct",
    ]
    rows: list[dict[str, Any]] = []
    for factor in factors:
        for rank, row in enumerate(sorted(company_rows, key=lambda item: -float(item.get(factor, 0)))[:10], start=1):
            rows.append(
                {
                    "factor": factor,
                    "rank": rank,
                    "ticker": row["ticker"],
                    "company_name": row["company_name"],
                    "score": row.get(factor, 0),
                    "analyst_bucket": row.get("analyst_bucket", ""),
                    "analyst_thesis": row.get("analyst_thesis", ""),
                }
            )
    return rows


def weighted_evidence_score(
    evidence: list[dict[str, Any]],
    allowed_modalities: set[str],
    object_quality_filter: set[str],
) -> float:
    score = 0.0
    for row in evidence:
        obj = str(row.get("object", "")).strip()
        if is_noisy_object(obj) or object_quality(obj) not in object_quality_filter:
            continue
        modality = str(row.get("modality", ""))
        if modality not in allowed_modalities:
            continue
        relation = str(row.get("relation_type", ""))
        confidence = float(row.get("confidence_score") or 0)
        score += MODALITY_WEIGHTS.get(modality, 0.35) * RELATION_WEIGHTS.get(relation, 0.5) * max(confidence, 0.25)
    return round(score, 3)


def object_quality(obj: str) -> str:
    lower = obj.lower().strip()
    if not lower or is_noisy_object(obj):
        return "noise"
    if lower in GEOGRAPHY_OBJECTS:
        return "geography"
    if lower.endswith(" dependency class") or lower.endswith(" concentration class") or " class" in lower:
        return "dependency_class"
    if re.search(r"\b(customer [a-z]|supplier [a-z]|major customers|significant customers|channel partners)\b", lower):
        return "anonymous_counterparty"
    if re.search(r"\b(inc|corp|corporation|company|co\.|ltd|limited|llc|plc|n\.v\.|s\.a\.|gmbh|b\.v\.|pte|lp)\b", obj, flags=re.I):
        return "named_entity"
    words = obj.split()
    titleish = sum(1 for word in words if word[:1].isupper() or word.isupper())
    if len(words) >= 2 and titleish >= max(2, len(words) - 1):
        return "named_entity"
    return "dependency_class"


def analyst_bucket(row: dict[str, Any]) -> str:
    if float(row["capex_beneficiary_pct"]) >= 80 and int(row["power_infra_relation_count"]) + int(row["cloud_data_relation_count"]) >= 20:
        return "capex_beneficiary_watch"
    if float(row["customer_concentration_pct"]) >= 80:
        return "customer_concentration_watch"
    if float(row["dependency_risk_pct"]) >= 80 and float(row["chokepoint_exposure_pct"]) >= 65:
        return "bottleneck_exposed_dependency_taker"
    if float(row["fragility_pct"]) >= 80 and int(row["risk_hypothetical_count"]) >= 25:
        return "fragility_watch"
    if float(row["operating_dependency_pct"]) >= 80:
        return "operating_dependency_watch"
    if float(row["evidence_quality_pct"]) <= 25 and int(row["evidence_count"]) >= 100:
        return "noisy_disclosure_review"
    return "balanced_monitor"


def analyst_thesis(row: dict[str, Any]) -> str:
    ticker = row["ticker"]
    bucket = row.get("analyst_bucket", "balanced_monitor")
    named = row.get("top_named_counterparties", "")
    geos = row.get("top_geographies", "")
    if bucket == "capex_beneficiary_watch":
        return f"{ticker}: capex beneficiary screen; disclosures cluster around data center, power, grid, cloud, or infrastructure buildout."
    if bucket == "customer_concentration_watch":
        return f"{ticker}: concentration screen; customer/concentration evidence is high. Review named customers and revenue dependence."
    if bucket == "bottleneck_exposed_dependency_taker":
        return f"{ticker}: dependency taker with exposure to common chokepoints; key counterparties include {named[:120]}."
    if bucket == "fragility_watch":
        return f"{ticker}: high forward-looking/risk disclosure mix; treat as monitoring signal until operating evidence strengthens."
    if bucket == "operating_dependency_watch":
        return f"{ticker}: operating dependency screen with current-fact evidence; named counterparties include {named[:120]}."
    if bucket == "noisy_disclosure_review":
        return f"{ticker}: high volume but lower evidence quality; inspect parser/entity noise before using as investment signal."
    if geos:
        return f"{ticker}: balanced monitor with geography exposure around {geos[:100]}."
    return f"{ticker}: balanced monitor; no single factor dominates the SEC-derived scorecard."


def bottleneck_theme(obj: str, relation_counts: Counter) -> str:
    lower = obj.lower()
    if lower in GEOGRAPHY_OBJECTS:
        return "geographic_exposure"
    if relation_counts.get("foundry_dependency") or "semiconductor" in lower or "tsmc" in lower:
        return "semiconductor_chokepoint"
    if relation_counts.get("cloud_or_hosting_dependency") or "cloud" in lower:
        return "cloud_platform"
    if relation_counts.get("power_or_utility_dependency") or "power" in lower or "electric" in lower:
        return "power_grid"
    if relation_counts.get("data_center_dependency") or "data center" in lower:
        return "data_center_capacity"
    if relation_counts.get("customer_dependency") or relation_counts.get("concentration_risk"):
        return "demand_concentration"
    if relation_counts.get("facility_or_geographic_exposure"):
        return "geographic_exposure"
    return "general_dependency"


def bottleneck_read(obj: str, relation_counts: Counter, modality_counts: Counter, quality: str) -> str:
    current = modality_counts.get("current_fact", 0) + modality_counts.get("strategic", 0)
    risk = modality_counts.get("risk_hypothetical", 0) + modality_counts.get("forward_looking", 0)
    top_relation = relation_counts.most_common(1)[0][0] if relation_counts else "dependency"
    if quality == "geography":
        return f"Geography exposure, not a company bottleneck; mostly {top_relation} language."
    if quality == "dependency_class":
        return f"Class-level object; useful for theme sizing but weak for named bottleneck ranking."
    if current >= risk:
        return f"Mostly current disclosed {top_relation}; candidate operating chokepoint."
    return f"Mostly risk-language {top_relation}; monitor, but do not treat as current operating dependency."


def top_objects(
    evidence: list[dict[str, Any]],
    limit: int,
    qualities: set[str] | None = None,
) -> list[str]:
    counts = Counter()
    for row in evidence:
        obj = str(row.get("object", "")).strip()
        if not obj or is_noisy_object(obj):
            continue
        quality = object_quality(obj)
        if qualities and quality not in qualities:
            continue
        counts[obj] += 1
    return [obj for obj, _ in counts.most_common(limit)]


def is_noisy_object(obj: str) -> bool:
    lower = obj.lower().strip()
    if lower in NOISY_OBJECTS:
        return True
    return lower.startswith(NOISY_OBJECT_PREFIXES)


def capex_role_score(role: str) -> float:
    role_l = role.lower()
    hits = sum(1 for term in CAPEX_BENEFICIARY_ROLE_TERMS if term in role_l)
    return min(2.0, 0.55 * hits)


def attach_percentiles(rows: list[dict[str, Any]], score_fields: list[str]) -> None:
    for field in score_fields:
        values = sorted(float(row.get(field, 0) or 0) for row in rows)
        n = len(values)
        pct_field = field.replace("_score", "_pct")
        for row in rows:
            value = float(row.get(field, 0) or 0)
            below_or_equal = sum(1 for item in values if item <= value)
            row[pct_field] = round(100 * below_or_equal / max(n, 1), 1)


def factor_definitions() -> dict[str, str]:
    return {
        "dependency_risk_score": "Weighted mix of operating dependency, risk disclosure, chokepoint exposure, customer concentration, and geography risk.",
        "operating_dependency_score": "Current/strategic/historical dependency evidence weighted toward named counterparties and geographies.",
        "chokepoint_exposure_score": "Log exposure to objects that multiple companies also disclose as dependencies.",
        "customer_concentration_score": "Customer dependency and concentration-risk evidence.",
        "capex_beneficiary_score": "Role- and disclosure-based signal for data center, power, grid, cloud, optical, server, or semicap beneficiaries.",
        "fragility_score": "Forward-looking plus risk-hypothetical evidence divided by current/strategic facts.",
        "evidence_quality_score": "Named-object ratio, named current-fact evidence, and extraction confidence.",
        "investment_relevance_score": "Composite screen intended to rank items for analyst review, not a trading signal.",
    }


def write_markdown_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Analyst Lens Report",
        "",
        f"Run: `{summary['run_id']}`",
        "",
        "## Scale",
        "",
        f"- Companies: {summary['company_count']}",
        f"- Evidence rows: {summary['evidence_count']}",
        f"- Graph edges: {summary['edge_count']}",
        f"- Bottleneck candidates: {summary['bottleneck_count']}",
        "",
        "## Top Company Screens",
        "",
    ]
    for row in summary["top_company_rows"][:12]:
        lines.append(
            f"- {row['ticker']}: {row['analyst_bucket']}; investment relevance pct {row['investment_relevance_pct']}. {row['analyst_thesis']}"
        )
    lines.extend(["", "## Top Bottleneck Screens", ""])
    for row in summary["top_bottleneck_rows"][:12]:
        lines.append(
            f"- {row['object']}: {row['theme']}; dependent companies {row['dependent_company_count']}; {row['analyst_read']}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def average(values: Iterable[float]) -> float:
    materialized = list(values)
    if not materialized:
        return 0.0
    return sum(materialized) / len(materialized)


def safe_ratio(numerator: int | float, denominator: int | float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


if __name__ == "__main__":
    raise SystemExit(main())
