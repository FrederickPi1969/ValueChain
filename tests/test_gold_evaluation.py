from valuechain.evaluation import evaluate_canonical_relationships


def test_gold_evaluation_reports_precision_recall_and_forbidden_edges() -> None:
    gold = {
        "version": "test",
        "scope": {"company": "NVIDIA Corporation", "relationship_family": "supply_chain"},
        "expected_relationships": [
            {"supplier": "TSMC", "customer": "NVIDIA Corporation", "relationship_type": "supplies_to"},
        ],
        "forbidden_relationships": [
            {"supplier": "Microsoft Corporation", "customer": "NVIDIA Corporation", "relationship_type": "supplies_to"},
        ],
    }
    rows = [
        {"supplier_name": "TSMC", "customer_name": "NVIDIA Corporation", "relationship_type": "supplies_to", "relationship_family": "supply_chain"},
        {"supplier_name": "Microsoft Corporation", "customer_name": "NVIDIA Corporation", "relationship_type": "supplies_to", "relationship_family": "supply_chain"},
    ]
    result = evaluate_canonical_relationships(rows, gold)
    assert result["precision"] == 0.5
    assert result["recall"] == 1.0
    assert result["forbidden_detected_count"] == 1


def test_gold_evaluation_detects_reversed_direction() -> None:
    gold = {"scope": {"company": "NVIDIA Corporation", "relationship_family": "supply_chain"}, "expected_relationships": [{"supplier": "TSMC", "customer": "NVIDIA Corporation", "relationship_type": "supplies_to"}]}
    result = evaluate_canonical_relationships(
        [{"supplier_name": "NVIDIA Corporation", "customer_name": "TSMC", "relationship_type": "supplies_to", "relationship_family": "supply_chain"}], gold
    )
    assert result["direction_error_count"] == 1
