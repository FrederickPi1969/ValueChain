from pathlib import Path

from valuechain.human_review import apply_human_reviews, inherit_prior_reviews, read_review_csv


def test_human_review_marks_rejected_without_removing_candidate(tmp_path: Path) -> None:
    review_csv = tmp_path / "review.csv"
    review_csv.write_text("relationship_id,review_status,review_notes\nr1,rejected,competition list\n", encoding="utf-8")
    reviewed = apply_human_reviews([{"relationship_id": "r1"}, {"relationship_id": "r2"}], read_review_csv(review_csv))
    assert [row["review_status"] for row in reviewed] == ["rejected", "unreviewed"]


def test_human_review_transfers_to_new_run_by_normalized_relationship_fingerprint(tmp_path: Path) -> None:
    review_csv = tmp_path / "review.csv"
    review_csv.write_text('relationship_id,supplier,customer,relationship_type,review_status\nold,"Micron Technology, Inc",NVIDIA Corporation,supplies_to,accepted\n', encoding="utf-8")
    reviewed = apply_human_reviews([{"relationship_id": "new", "supplier_name": "Micron Technology Inc.", "customer_name": "NVIDIA Corporation", "relationship_type": "supplies_to"}], read_review_csv(review_csv))
    assert reviewed[0]["review_status"] == "accepted"


def test_prior_foundry_confirmation_survives_supply_schema_consolidation() -> None:
    inherited = inherit_prior_reviews(
        [{"relationship_id": "new", "supplier_name": "TSMC", "customer_name": "NVIDIA", "relationship_type": "supplies_to", "review_status": "unreviewed"}],
        [{"relationship_id": "old", "supplier_name": "TSMC", "customer_name": "NVIDIA", "relationship_type": "manufactures_for", "review_status": "accepted"}],
    )
    assert inherited[0]["review_status"] == "accepted"
    assert inherited[0]["decision"] == "accept"
