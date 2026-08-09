"""Versioned, executable relationship ontology contract."""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from valuechain.config import Settings, load_ontology


@lru_cache(maxsize=1)
def relationship_contract() -> dict[str, Any]:
    contract = load_ontology(Settings()).get("relationship_contract", {})
    if not contract.get("canonical_types") or not contract.get("risk_flags"):
        raise ValueError("ontology.yaml needs relationship_contract canonical_types and risk_flags")
    return contract


def ontology_version() -> str:
    return str(load_ontology(Settings()).get("version", "unknown"))


def blocking_risk_flags() -> set[str]:
    return {name for name, spec in relationship_contract()["risk_flags"].items() if spec.get("blocks_cross_filing_auto_accept")}


def raw_relation_types() -> set[str]:
    return set(load_ontology(Settings()).get("relation_types", {}))


def raw_relation_spec(relation_type: str) -> dict[str, Any] | None:
    return relationship_contract()["raw_relation_mappings"].get(relation_type)


def canonical_relation_for_raw(relation_type: str) -> tuple[str, str] | None:
    spec = raw_relation_spec(relation_type)
    return (str(spec["family"]), str(spec["canonical_type"])) if spec else None


def orientation_for_raw(relation_type: str) -> str:
    return str((raw_relation_spec(relation_type) or {}).get("orientation", "object_to_subject"))


def category_for_raw(relation_type: str) -> str:
    return str((raw_relation_spec(relation_type) or {}).get("category", ""))


def validate_canonical_relationship(row: dict[str, Any]) -> list[str]:
    spec = relationship_contract()["canonical_types"].get(str(row.get("relationship_type", "")))
    if not spec:
        return ["unknown_canonical_relationship_type"]
    errors: list[str] = []
    for field in ("relationship_family", "source_role", "target_role"):
        expected = spec.get(field.replace("relationship_", "")) if field == "relationship_family" else spec.get(field)
        if expected and row.get(field) and row.get(field) != expected:
            errors.append(f"{field}_violates_ontology")
    unknown = set(row.get("risk_flags", [])) - set(relationship_contract()["risk_flags"])
    if unknown:
        errors.append("unknown_risk_flag")
    return errors
