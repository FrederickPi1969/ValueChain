"""Small, deterministic regression evaluator for manually curated gold relations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from valuechain.edge_quality import object_key


def load_gold(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("expected_relationships"), list):
        raise ValueError("Gold set needs expected_relationships.")
    if not isinstance(payload.get("forbidden_relationships", []), list):
        raise ValueError("Gold set forbidden_relationships must be a list.")
    return payload


def evaluate_canonical_relationships(relationships: list[dict[str, Any]], gold: dict[str, Any]) -> dict[str, Any]:
    scope = gold.get("scope", {})
    company_key = object_key(str(scope.get("company", "")))
    family = str(scope.get("relationship_family", ""))
    scoped = [
        row for row in relationships
        if (not family or row.get("relationship_family") == family)
        and (not company_key or company_key in {object_key(str(row.get("supplier_name", ""))), object_key(str(row.get("customer_name", "")))})
    ]
    predicted = {relationship_key(row) for row in scoped}
    expected = {relationship_key(row) for row in gold["expected_relationships"]}
    forbidden = {relationship_key(row) for row in gold.get("forbidden_relationships", [])}
    matched = expected & predicted
    false_negatives = expected - predicted
    false_positives = predicted - expected
    forbidden_detected = forbidden & predicted
    direction_errors = {
        key for key in false_negatives
        if reverse_key(key) in predicted
    }
    entity_resolution_mismatches = [
        {"expected": display_key(key), "actual": display_key(actual)}
        for key in false_negatives
        for actual in false_positives
        if key[1:] == actual[1:] and key[2:] == actual[2:]
    ]
    tp, fp, fn = len(matched), len(false_positives), len(false_negatives)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "gold_version": gold.get("version", "unknown"),
        "scope": scope,
        "expected_relationship_count": len(expected),
        "predicted_relationship_count": len(predicted),
        "matched_relationship_count": tp,
        "false_positive_count": fp,
        "false_negative_count": fn,
        "forbidden_detected_count": len(forbidden_detected),
        "direction_error_count": len(direction_errors),
        "entity_resolution_mismatch_count": len(entity_resolution_mismatches),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "matched": [display_key(key) for key in sorted(matched)],
        "false_positives": [display_key(key) for key in sorted(false_positives)],
        "false_negatives": [display_key(key) for key in sorted(false_negatives)],
        "forbidden_detected": [display_key(key) for key in sorted(forbidden_detected)],
        "direction_errors": [display_key(key) for key in sorted(direction_errors)],
        "entity_resolution_mismatches": entity_resolution_mismatches,
    }


def relationship_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        object_key(str(row.get("supplier_name") or row.get("supplier") or "")),
        object_key(str(row.get("customer_name") or row.get("customer") or "")),
        str(row.get("relationship_type", "")),
    )


def reverse_key(key: tuple[str, str, str]) -> tuple[str, str, str]:
    return (key[1], key[0], key[2])


def display_key(key: tuple[str, str, str]) -> dict[str, str]:
    return {"supplier": key[0], "customer": key[1], "relationship_type": key[2]}
