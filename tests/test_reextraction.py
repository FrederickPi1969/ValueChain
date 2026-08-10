from valuechain.reextraction import compare_reextraction


def test_reextraction_comparison_separates_assertion_and_relationship_deltas() -> None:
    summary = compare_reextraction(
        [{"evidence_id": "old"}],
        [{"evidence_id": "new", "product_or_service": "memory"}],
        [{"relationship_id": "r1", "source_entity_name": "Micron", "target_entity_name": "NVIDIA", "relationship_type": "supplies_to", "modality": "current_fact", "relationship_family": "supply_chain", "decision": "accept"}],
        [{"relationship_id": "r1", "source_entity_name": "Micron", "target_entity_name": "NVIDIA", "relationship_type": "supplies_to", "modality": "current_fact", "relationship_family": "supply_chain", "product_or_service": "memory"}],
    )
    assert summary["preview_only_assertions"] == 1
    assert summary["current_only_assertions"] == 1
    assert summary["semantic_relationship_overlap"] == 1
    assert summary["shared_relationships_with_changed_product"] == 1
    assert summary["prior_decision_ids_not_present_in_preview"] == 0
