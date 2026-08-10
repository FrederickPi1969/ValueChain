from valuechain.evidence_audit import apply_latest_audit_decisions, automatic_cross_filing_audit, audit_payload, build_direction_correction_proposals, evidence_for_relationship, merge_audit_history, migrate_relationship_evidence_ids, normalize_audit, normalize_follow_up


def test_audit_payload_only_includes_relationship_evidence() -> None:
    payload = audit_payload({"supplier_name": "TSMC", "customer_name": "NVIDIA", "relationship_type": "manufactures_for"}, [{"form": "10-K", "evidence_text": "x" * 7000}])
    assert payload["proposed_relationship"]["supplier"] == "TSMC"
    assert len(payload["evidence"][0]["text"]) == 6000


def test_normalize_audit_rejects_unknown_decision() -> None:
    row = normalize_audit({"relationship_id": "r1"}, {"decision": "maybe", "confidence": 9}, "test")
    assert row["decision"] == "review"
    assert row["confidence"] == 1.0


def test_normalize_audit_keeps_product_metadata() -> None:
    row = normalize_audit({"relationship_id": "r1"}, {"decision": "accept", "product_or_service": "memory"}, "test")
    assert row["product_or_service"] == "memory"


def test_relationship_evidence_is_bound_to_its_supplier_not_shared_passage() -> None:
    rows = evidence_for_relationship(
        {"supplier_name": "Micron Technology, Inc", "customer_name": "NVIDIA", "evidence_ids": ["p1"]},
        {"p1": [{"object": "Taiwan Semiconductor Manufacturing Company Limited"}, {"object": "Micron Technology, Inc"}]},
    )
    assert rows == [{"object": "Micron Technology, Inc"}]


def test_audit_can_load_assertion_level_evidence_ids() -> None:
    rows = evidence_for_relationship(
        {"supplier_name": "Micron Technology, Inc", "customer_name": "NVIDIA", "evidence_ids": ["evidence:micron"]},
        {"evidence:micron": [{"object": "Micron Technology, Inc"}]},
    )
    assert rows == [{"object": "Micron Technology, Inc"}]


def test_direction_correction_migrates_legacy_passage_evidence_ids() -> None:
    migrated = migrate_relationship_evidence_ids(
        [{"supplier_name": "AMD", "customer_name": "OpenAI", "evidence_ids": ["p1"]}],
        [{"passage_id": "p1", "subject": "AMD", "object": "OpenAI", "relation_type": "supplier_dependency", "direction": "subject_depends_on_object"}],
    )
    assert migrated[0]["evidence_ids"][0].startswith("evidence:")


def test_follow_up_normalization_keeps_valid_recommendation() -> None:
    row = normalize_follow_up({"follow_up_reason": "The issuer purchases memory.", "recommended_decision": "accept"})
    assert row["recommended_decision"] == "accept"


def test_cross_filing_audit_is_automatically_accepted() -> None:
    audit = automatic_cross_filing_audit({"relationship_id": "r1", "relationship_type": "supplies_to", "source_accession_numbers": ["a1", "a2"]})
    assert audit["decision"] == "accept"
    assert audit["decision_source"] == "cross_filing_rule"


def test_reverse_audit_creates_unconfirmed_reverse_candidate() -> None:
    original = {"relationship_id": "wrong", "supplier_entity_id": "openai", "supplier_name": "OpenAI", "customer_entity_id": "amd", "customer_name": "AMD", "source_entity_id": "openai", "source_entity_name": "OpenAI", "source_role": "supplier", "target_entity_id": "amd", "target_entity_name": "AMD", "target_role": "customer", "relationship_type": "supplies_to", "relationship_family": "supply_chain", "modality": "current_fact", "risk_flags": ["direction_anomaly"]}
    rows = build_direction_correction_proposals([original], [{"relationship_id": "wrong", "direction_assessment": "reverse", "reason": "Evidence names AMD as supplier."}])
    assert rows[0]["source_entity_name"] == "AMD"
    assert rows[0]["target_entity_name"] == "OpenAI"
    assert rows[0]["review_status"] == "needs_review"


def test_audit_product_is_persisted_on_canonical_relationship():
    rows = apply_latest_audit_decisions([{"relationship_id": "r1", "risk_flags": ["product_or_service_not_extracted"]}], [{"relationship_id": "r1", "decision": "accept", "reason": "Explicit purchase.", "product_or_service": "memory"}])
    assert rows[0]["product_or_service"] == "memory"
    assert "product_or_service_not_extracted" not in rows[0]["risk_flags"]


def test_latest_valid_history_event_becomes_current_verdict() -> None:
    current = [{"relationship_id": "r1", "decision": "reject", "follow_up_reason": "Second-pass LLM error: bad JSON"}]
    prior = [{"relationship_id": "r1", "decision": "accept", "reason": "Evidence supports it.", "model_version": "model"}]
    assert merge_audit_history(current, prior)[0]["decision"] == "accept"
