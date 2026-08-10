"""Append-only lineage events connecting SEC evidence to visible graph edges."""
from __future__ import annotations

from hashlib import sha256
from datetime import datetime, timezone
from typing import Any

from valuechain.ontology import ontology_version, validate_canonical_relationship


def relationship_lineage_events(relationships: list[dict[str, Any]], stage: str, previous: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in relationships:
        relationship_id = str(row.get("relationship_id", ""))
        before = (previous or {}).get(relationship_id, {})
        payload = {"stage": stage, "relationship_id": relationship_id, "evidence_ids": row.get("evidence_ids", []), "source_accession_numbers": row.get("source_accession_numbers", []), "risk_flags": row.get("risk_flags", []), "decision": row.get("decision", "review"), "decision_source": row.get("decision_source", "pending_review"), "actor": "human" if row.get("decision_source") == "human_review" else "system_or_llm", "before_state": {"review_status": before.get("review_status"), "decision": before.get("decision")}, "after_state": {"review_status": row.get("review_status"), "decision": row.get("decision")}, "direction_correction_of": row.get("direction_correction_of", ""), "ontology_validation": validate_canonical_relationship(row)}
        payload["event_id"] = f"lineage:{sha256(repr(sorted(payload.items())).encode()).hexdigest()[:20]}"
        payload["ontology_version"] = ontology_version()
        payload["created_at"] = datetime.now(timezone.utc).isoformat()
        events.append(payload)
    return events


def merge_lineage_history(prior: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep local lineage append-only while making repeated commands idempotent."""
    merged = {str(row.get("event_id", "")): row for row in prior}
    merged.update({str(row.get("event_id", "")): row for row in current})
    return sorted(merged.values(), key=lambda row: (str(row.get("relationship_id", "")), str(row.get("created_at", "")), str(row.get("event_id", ""))))
