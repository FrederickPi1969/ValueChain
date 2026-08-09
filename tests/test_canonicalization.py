from valuechain.canonicalization import build_canonical_layer, relationship_review_queue
from valuechain.models import Company, RelationEvidence
from valuechain.human_review import inherit_prior_reviews


def record(object_name: str, relation_type: str) -> RelationEvidence:
    return RelationEvidence(
        subject="NVIDIA Corporation", object=object_name, relation_type=relation_type,
        direction="subject_depends_on_object", modality="current_fact", certainty="high",
        temporal_scope="as_disclosed", evidence_text="We rely on the named counterparty.",
        confidence_score=0.9, extractor_model_version="rules", ticker="NVDA", cik="1", form="10-K",
        filing_date="2026-02-25", accepted_timestamp="", accession_number="a1",
        source_document_url="https://example.com", source_section="item_1", passage_id=f"p-{relation_type}",
        paragraph_offset=0, parser_name="parser", parser_version="0.1",
    )


def test_canonical_layer_reverses_upstream_dependency_to_supply_direction() -> None:
    entities, relationships, diagnostics = build_canonical_layer(
        [Company("NVDA", "NVIDIA Corporation", cik="1")],
        [record("TSMC", "foundry_dependency")],
    )
    assert len(entities) == 2
    assert relationships[0]["supplier_name"] == "Taiwan Semiconductor Manufacturing Company Limited"
    assert relationships[0]["customer_name"] == "NVIDIA Corporation"
    assert relationships[0]["relationship_type"] == "supplies_to"
    assert relationships[0]["categories"] == ["semiconductor foundry"]
    assert diagnostics[0]["status"] == "canonicalized"


def test_canonical_layer_keeps_generic_object_as_a_diagnostic() -> None:
    _, relationships, diagnostics = build_canonical_layer(
        [Company("NVDA", "NVIDIA Corporation", cik="1")],
        [record("supplier(s)", "supplier_dependency")],
    )
    assert relationships == []
    assert diagnostics[0]["status"] == "unresolved_or_generic_counterparty"


def test_canonical_layer_auto_verifies_two_independent_issuer_disclosures() -> None:
    first = record("TSMC", "foundry_dependency")
    second = record("TSMC", "foundry_dependency")
    second.accession_number = "a2"
    _, relationships, _ = build_canonical_layer(
        [Company("NVDA", "NVIDIA Corporation", cik="1"), Company("TSM", "Taiwan Semiconductor Manufacturing Company Limited", cik="2")],
        [first, second],
    )
    assert relationships[0]["verification_status"] == "cross_filing_verified"
    assert relationships[0]["review_status"] == "accepted"
    assert relationships[0]["decision"] == "accept"


def test_cross_filing_does_not_auto_accept_a_direction_risk() -> None:
    first = record("Meta Platforms, Inc.", "data_center_dependency")
    second = record("Meta Platforms, Inc.", "data_center_dependency")
    first.evidence_text = second.evidence_text = "Each customer intends to deploy AMD data center GPUs."
    second.accession_number = "a2"
    _, relationships, _ = build_canonical_layer([Company("AMD", "Advanced Micro Devices Inc.", cik="1")], [first, second])
    assert "direction_anomaly" in relationships[0]["risk_flags"]
    assert relationships[0]["review_status"] == "unreviewed"


def test_canonical_layer_blocks_non_entity_table_fragments() -> None:
    _, relationships, diagnostics = build_canonical_layer(
        [Company("AMD", "Advanced Micro Devices Inc.", cik="1")],
        [record("Domestic Subsidiaries", "subsidiary_or_control")],
    )
    assert relationships == []
    assert diagnostics[0]["status"] == "non_entity_blocklisted"


def test_plausible_legal_entity_is_not_blocklisted_by_a_generic_suffix() -> None:
    _, relationships, _ = build_canonical_layer(
        [Company("AMD", "Advanced Micro Devices Inc.", cik="1")], [record("Networks, Inc.", "supplier_dependency")]
    )
    assert len(relationships) == 1


def test_canonical_layer_records_direct_parent_for_expandable_entities() -> None:
    control = record("Xilinx, Inc.", "subsidiary_or_control")
    control.subject = "Advanced Micro Devices Inc."
    entities, relationships, _ = build_canonical_layer(
        [Company("AMD", "Advanced Micro Devices Inc.", cik="1")], [control]
    )
    xilinx = next(row for row in entities if row["canonical_name"] == "Xilinx, Inc")
    assert relationships[0]["relationship_family"] == "ownership_control"
    assert xilinx["parent_name"] == "Advanced Micro Devices Inc."


def test_license_is_commercial_not_supply_chain() -> None:
    license_record = record("Groq, Inc.", "licensing_dependency")
    license_record.evidence_text = "We entered a license agreement with Groq, Inc."
    _, relationships, _ = build_canonical_layer([Company("NVDA", "NVIDIA Corporation", cik="1")], [license_record])
    assert relationships[0]["relationship_family"] == "commercial_relationship"
    assert relationships[0]["relationship_type"] == "licenses_to"


def test_competitor_context_is_retained_as_flagged_candidate_not_silently_dropped() -> None:
    competitor = record("Intel Corporation", "supplier_dependency")
    competitor.evidence_text = "Intel Corporation is a competitor in our market."
    _, relationships, diagnostics = build_canonical_layer([Company("AMD", "NVIDIA Corporation", cik="1")], [competitor])
    assert len(relationships) == 1
    assert "competitor_or_market_context" in relationships[0]["risk_flags"]
    assert diagnostics[0]["status"] == "canonicalized"


def test_ownership_uses_generic_endpoint_roles_and_candidates_are_prioritized() -> None:
    control = record("Xilinx, Inc.", "subsidiary_or_control")
    control.subject = "Advanced Micro Devices Inc."
    _, relationships, _ = build_canonical_layer([Company("AMD", "Advanced Micro Devices Inc.", cik="1")], [control])
    assert relationships[0]["source_role"] == "controller"
    assert relationships[0]["target_role"] == "controlled_entity"
    assert relationship_review_queue(relationships)[0]["relationship_id"] == relationships[0]["relationship_id"]


def test_customer_deployment_is_not_reversed_into_cloud_supply() -> None:
    deployment = record("Meta Platforms, Inc.", "data_center_dependency")
    deployment.subject = "Advanced Micro Devices Inc."
    deployment.evidence_text = "Each customer intends to deploy AMD data center GPUs."
    _, relationships, _ = build_canonical_layer([Company("AMD", "Advanced Micro Devices Inc.", cik="1")], [deployment])
    # The canonical layer no longer changes direction from one surface phrase;
    # it retains the extraction proposal and exposes the anomaly to the audit.
    assert "direction_anomaly" in relationships[0]["risk_flags"]


def test_prior_review_never_crosses_a_reversed_direction() -> None:
    current = [{"relationship_id": "new", "supplier_name": "OpenAI", "customer_name": "AMD", "source_entity_name": "OpenAI", "target_entity_name": "AMD", "relationship_type": "supplies_to", "relationship_family": "supply_chain", "modality": "current_fact", "review_status": "unreviewed"}]
    prior = [{"supplier_name": "AMD", "customer_name": "OpenAI", "source_entity_name": "AMD", "target_entity_name": "OpenAI", "relationship_type": "supplies_to", "relationship_family": "supply_chain", "modality": "current_fact", "review_status": "accepted"}]
    assert inherit_prior_reviews(current, prior)[0]["review_status"] == "unreviewed"
