"""Import human canonical-relationship decisions without destroying raw candidates."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from valuechain.dashboard import canonical_network_edges
from valuechain.edge_quality import object_key


VALID_STATUSES = {"accepted", "rejected", "needs_review", "unreviewed"}

LEGACY_SUPPLY_RELATION_TYPES = {
    "supplies_to", "manufactures_for", "assembles_for", "provides_cloud_service_to",
    "provides_data_center_capacity_to", "provides_utility_to", "provides_network_to",
    "distributes_for", "licenses_to",
}


def review_relation_type(value: str) -> str:
    """Keep reviews valid after supply subtypes are consolidated to supplies_to."""
    return "supplies_to" if value in LEGACY_SUPPLY_RELATION_TYPES else value


def read_review_csv(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    decisions: dict[str, dict[str, str]] = {}
    for row in rows:
        relationship_id = str(row.get("relationship_id", "")).strip()
        status = str(row.get("review_status", "")).strip().lower()
        if relationship_id and status in VALID_STATUSES:
            decisions[relationship_id] = {
                "status": status, "notes": str(row.get("review_notes", "")).strip(),
                "supplier": str(row.get("supplier", "")).strip(), "customer": str(row.get("customer", "")).strip(),
                "relationship_type": str(row.get("relationship_type", "")).strip(),
            }
    return decisions


def apply_human_reviews(relationships: list[dict[str, Any]], decisions: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    by_fingerprint: dict[tuple[str, str, str, str, str], dict[str, str]] = {}
    for decision in decisions.values():
        source = decision.get("source", decision.get("supplier", ""))
        target = decision.get("target", decision.get("customer", ""))
        relation_type = decision.get("relationship_type", "")
        family = decision.get("relationship_family", "")
        modality = decision.get("modality", "")
        if source and target and relation_type:
            by_fingerprint[(object_key(source), object_key(target), review_relation_type(relation_type), family, modality)] = decision
    rows: list[dict[str, Any]] = []
    for relationship in relationships:
        row = dict(relationship)
        fingerprint = (
            object_key(str(row.get("source_entity_name", row.get("supplier_name", "")))),
            object_key(str(row.get("target_entity_name", row.get("customer_name", "")))),
            review_relation_type(str(row.get("relationship_type", ""))),
            str(row.get("relationship_family", "")), str(row.get("modality", "")),
        )
        automatic = {
            "status": str(row.get("review_status", "unreviewed")),
            "notes": str(row.get("decision_reason", "")),
        }
        imported = decisions.get(str(row.get("relationship_id", ""))) or by_fingerprint.get(fingerprint) or by_fingerprint.get(fingerprint[:3] + ("", ""))
        # A blank/unreviewed CSV row must not silently undo a programmatic,
        # cross-filing acceptance. An explicit Reject or Review may override it.
        review = imported if imported and imported.get("status") != "unreviewed" else automatic
        row["human_review"] = review
        row["review_status"] = review["status"]
        rows.append(row)
    return rows


def inherit_prior_reviews(relationships: list[dict[str, Any]], prior_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Carry confirmed human/LLM-reviewed facts across a rerun or schema migration."""
    decisions: dict[str, dict[str, str]] = {}
    for index, prior in enumerate(prior_rows):
        status = str(prior.get("review_status", "unreviewed"))
        if status == "unreviewed":
            continue
        review = prior.get("human_review") or {}
        decisions[f"prior:{index}"] = {
            "status": status,
            "notes": str(review.get("notes") or prior.get("decision_reason") or "Inherited from a prior reviewed run."),
            "supplier": str(prior.get("supplier_name", "")),
            "customer": str(prior.get("customer_name", "")),
            "source": str(prior.get("source_entity_name", prior.get("supplier_name", ""))),
            "target": str(prior.get("target_entity_name", prior.get("customer_name", ""))),
            "relationship_type": str(prior.get("relationship_type", "")),
            "relationship_family": str(prior.get("relationship_family", "")),
            "modality": str(prior.get("modality", "")),
        }
    inherited = apply_human_reviews(relationships, decisions)
    for row in inherited:
        if row.get("human_review", {}).get("notes") == "Inherited from a prior reviewed run." or str(row.get("human_review", {}).get("notes", "")).startswith("Inherited from"):
            if row.get("review_status") == "accepted":
                row["decision"] = "accept"
                row["decision_source"] = "prior_review"
                row["decision_reason"] = str(row["human_review"]["notes"])
    return inherited


def publish_human_review_to_dashboard(dashboard_path: Path, reviewed_relationships: list[dict[str, Any]]) -> bool:
    if not dashboard_path.exists():
        return False
    payload = json.loads(dashboard_path.read_text(encoding="utf-8"))
    accepted = [row for row in reviewed_relationships if row.get("review_status") == "accepted"]
    payload["canonical_relationships"] = reviewed_relationships
    payload["network_edges"] = canonical_network_edges(reviewed_relationships)
    payload.setdefault("summary", {})["canonical_relationship_count"] = len(accepted)
    payload["summary"]["canonical_relationship_candidate_count"] = len(reviewed_relationships)
    payload["summary"]["network_edge_count"] = len(payload["network_edges"])
    payload["human_review_summary"] = {
        "accepted": len(accepted),
        "rejected": sum(1 for row in reviewed_relationships if row.get("review_status") == "rejected"),
        "needs_review": sum(1 for row in reviewed_relationships if row.get("review_status") == "needs_review"),
        "unreviewed": sum(1 for row in reviewed_relationships if row.get("review_status") == "unreviewed"),
    }
    dashboard_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return True
