from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


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
CUSTOMER_RELATIONS = {
    "customer_dependency",
    "concentration_risk",
}
GEOGRAPHY_OBJECTS = {
    "china",
    "united states",
    "taiwan",
    "europe",
    "japan",
    "south korea",
    "netherlands",
    "ireland",
    "israel",
    "singapore",
    "malaysia",
    "india",
    "hong kong",
    "canada",
    "australia",
}
NOISY_OBJECT_PREFIXES = (
    "contents ",
    "notes ",
    "item ",
    "part ",
    "table ",
)
NOISY_OBJECTS = {
    "entity name",
    "name of subsidiary",
    "business overview",
    "risk factors",
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
    summary = {
        "run_id": args.run_id,
        "company_count": len(companies),
        "evidence_count": len(evidence),
        "edge_count": len(edges),
        "bottleneck_count": len(bottlenecks),
        "top_company_rows": company_rows[:15],
        "top_bottleneck_rows": bottleneck_rows[:15],
    }

    write_csv(out_dir / "company_scorecard.csv", company_rows)
    write_csv(out_dir / "bottleneck_thesis.csv", bottleneck_rows)
    write_json(out_dir / "analyst_summary.json", summary)

    print(f"Wrote analyst lens outputs to {out_dir}")
    print(f"companies={len(company_rows)} evidence={len(evidence)} edges={len(edges)}")
    print("Top company screens:")
    for row in company_rows[:10]:
        print(
            row["ticker"],
            row["analyst_bucket"],
            "dependency_intensity=",
            row["dependency_intensity"],
            "chokepoint_exposure=",
            row["chokepoint_exposure"],
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
        if row.get("object")
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
        relation_counts = Counter(row.get("relation_type", "") for row in subject_evidence)
        modality_counts = Counter(row.get("modality", "") for row in subject_evidence)
        objects = {str(row.get("object", "")).strip() for row in subject_evidence if row.get("object")}
        avg_confidence = average(float(row.get("confidence_score") or 0) for row in subject_evidence)
        chokepoint_exposure = sum(bottleneck_weight.get(obj, 0) for obj in objects)
        dependency_intensity = round(
            math.log1p(len(subject_evidence)) + 0.5 * math.log1p(len(subject_edges)),
            3,
        )
        current = modality_counts.get("current_fact", 0)
        risk = modality_counts.get("risk_hypothetical", 0)
        forward = modality_counts.get("forward_looking", 0)
        fragility_ratio = round((risk + forward) / (current + 1), 3)
        supplier_count = sum(relation_counts.get(name, 0) for name in SUPPLIER_RELATIONS)
        cloud_data_count = sum(relation_counts.get(name, 0) for name in CLOUD_DATA_RELATIONS)
        power_infra_count = sum(relation_counts.get(name, 0) for name in POWER_INFRA_RELATIONS)
        customer_count = sum(relation_counts.get(name, 0) for name in CUSTOMER_RELATIONS)
        row = {
            "ticker": company.get("ticker", ""),
            "company_name": company_name,
            "role": company.get("role", ""),
            "priority": company.get("priority", ""),
            "evidence_count": len(subject_evidence),
            "edge_count": len(subject_edges),
            "unique_object_count": len(objects),
            "avg_confidence": round(avg_confidence, 3),
            "dependency_intensity": dependency_intensity,
            "fragility_ratio": fragility_ratio,
            "chokepoint_exposure": chokepoint_exposure,
            "supplier_relation_count": supplier_count,
            "cloud_data_relation_count": cloud_data_count,
            "power_infra_relation_count": power_infra_count,
            "customer_concentration_count": customer_count,
            "current_fact_count": current,
            "risk_hypothetical_count": risk,
            "forward_looking_count": forward,
            "strategic_count": modality_counts.get("strategic", 0),
            "analyst_bucket": analyst_bucket(
                role=company.get("role", ""),
                supplier_count=supplier_count,
                cloud_data_count=cloud_data_count,
                power_infra_count=power_infra_count,
                customer_count=customer_count,
                chokepoint_exposure=chokepoint_exposure,
                fragility_ratio=fragility_ratio,
            ),
            "top_dependency_objects": "; ".join(top_objects(subject_evidence, limit=8)),
        }
        rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            -float(row["dependency_intensity"]),
            -int(row["chokepoint_exposure"]),
            row["ticker"],
        ),
    )


def build_bottleneck_thesis(
    bottlenecks: list[dict[str, str]],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence_by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        obj = str(row.get("object", "")).strip()
        if obj:
            evidence_by_object[obj].append(row)

    rows: list[dict[str, Any]] = []
    for row in bottlenecks:
        obj = row.get("object", "")
        obj_evidence = evidence_by_object.get(obj, [])
        modality_counts = Counter(item.get("modality", "") for item in obj_evidence)
        relation_counts = Counter(item.get("relation_type", "") for item in obj_evidence)
        dependent_company_count = int(row.get("dependent_company_count") or 0)
        evidence_count = int(row.get("evidence_count") or 0)
        rows.append(
            {
                "object": obj,
                "theme": bottleneck_theme(obj, relation_counts),
                "dependent_company_count": dependent_company_count,
                "evidence_count": evidence_count,
                "current_fact_count": modality_counts.get("current_fact", 0),
                "risk_hypothetical_count": modality_counts.get("risk_hypothetical", 0),
                "forward_looking_count": modality_counts.get("forward_looking", 0),
                "relation_types": row.get("relation_types", ""),
                "subjects": row.get("subjects", ""),
                "analyst_read": bottleneck_read(obj, relation_counts, modality_counts),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -int(row["dependent_company_count"]),
            -int(row["evidence_count"]),
            row["object"],
        ),
    )


def analyst_bucket(
    role: str,
    supplier_count: int,
    cloud_data_count: int,
    power_infra_count: int,
    customer_count: int,
    chokepoint_exposure: int,
    fragility_ratio: float,
) -> str:
    role_l = role.lower()
    if any(term in role_l for term in ["power", "grid", "cooling", "construction", "data_center"]):
        return "capex_beneficiary_watch"
    if customer_count >= 30:
        return "customer_concentration_watch"
    if chokepoint_exposure >= 40 and supplier_count + cloud_data_count + power_infra_count >= 20:
        return "bottleneck_exposed_dependency_taker"
    if fragility_ratio >= 0.65 and supplier_count + cloud_data_count + power_infra_count >= 10:
        return "fragility_watch"
    if cloud_data_count + power_infra_count >= 20:
        return "infrastructure_dependency_watch"
    if supplier_count >= 20:
        return "supplier_dependency_watch"
    return "balanced_monitor"


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


def bottleneck_read(obj: str, relation_counts: Counter, modality_counts: Counter) -> str:
    current = modality_counts.get("current_fact", 0)
    risk = modality_counts.get("risk_hypothetical", 0)
    top_relation = relation_counts.most_common(1)[0][0] if relation_counts else "dependency"
    if current >= risk:
        return f"Mostly current disclosed {top_relation}; candidate operating exposure."
    return f"Mostly risk-language {top_relation}; candidate monitoring item, not yet operating fact."


def top_objects(evidence: list[dict[str, Any]], limit: int) -> list[str]:
    counts = Counter(
        obj
        for row in evidence
        if (obj := str(row.get("object", "")).strip()) and not is_noisy_object(obj)
    )
    return [obj for obj, _ in counts.most_common(limit)]


def is_noisy_object(obj: str) -> bool:
    lower = obj.lower().strip()
    if lower in NOISY_OBJECTS:
        return True
    if lower.endswith(" dependency class") or lower.endswith(" concentration class"):
        return True
    return lower.startswith(NOISY_OBJECT_PREFIXES)


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


def average(values) -> float:
    materialized = list(values)
    if not materialized:
        return 0.0
    return sum(materialized) / len(materialized)


if __name__ == "__main__":
    raise SystemExit(main())
