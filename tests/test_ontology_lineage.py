from valuechain.lineage import merge_lineage_history, relationship_lineage_events
from valuechain.ontology import blocking_risk_flags, validate_canonical_relationship
from valuechain.relation_rules import RELATION_PATTERNS
from valuechain.ontology import raw_relation_types


def test_ontology_marks_direction_as_blocking_but_product_gap_as_nonblocking():
    flags = blocking_risk_flags()
    assert "direction_anomaly" in flags
    assert "product_or_service_not_extracted" not in flags


def test_lineage_event_connects_candidate_to_evidence_and_contract():
    row = {"relationship_id": "r1", "relationship_type": "supplies_to", "relationship_family": "supply_chain", "source_role": "supplier", "target_role": "customer", "evidence_ids": ["p1"], "source_accession_numbers": ["a1"], "risk_flags": [], "decision": "review", "decision_source": "pending_review"}
    event = relationship_lineage_events([row], "canonical_refreshed")[0]
    assert event["evidence_ids"] == ["p1"]
    assert event["ontology_validation"] == []
    assert validate_canonical_relationship(row) == []


def test_every_rule_pattern_type_is_owned_by_the_ontology():
    assert {relation_type for relation_type, *_ in RELATION_PATTERNS} <= raw_relation_types()


def test_lineage_history_keeps_multiple_stages_and_deduplicates_replays():
    row = {"relationship_id": "r1", "relationship_type": "supplies_to", "relationship_family": "supply_chain", "source_role": "supplier", "target_role": "customer"}
    canonical = relationship_lineage_events([row], "canonical_refreshed")
    audited = relationship_lineage_events([{**row, "decision": "accept", "decision_source": "llm_relationship_audit"}], "audit_applied")
    assert len(merge_lineage_history(canonical, audited + canonical)) == 2
