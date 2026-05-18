import csv

from valuechain.models import RelationEvidence
from valuechain.pipeline import write_validation_sample


def test_write_validation_sample_adds_gold_review_columns(tmp_path) -> None:
    record = RelationEvidence(
        subject="NVIDIA Corporation",
        object="TSMC",
        relation_type="foundry_dependency",
        direction="subject_depends_on_object",
        modality="current_fact",
        certainty="high",
        temporal_scope="as_disclosed",
        evidence_text="We rely on TSMC for foundry capacity.",
        confidence_score=0.8,
        extractor_model_version="rules",
        ticker="NVDA",
        cik="0001045810",
        form="10-K",
        filing_date="2025-02-26",
        accepted_timestamp="",
        accession_number="a1",
        source_document_url="https://example.com",
        source_section="item_1_business",
        passage_id="p1",
        paragraph_offset=0,
        parser_name="parser",
        parser_version="0.1",
    )
    output = tmp_path / "validation.csv"
    write_validation_sample(output, [record])
    with output.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["gold_relation_present"] == ""
    assert rows[0]["relation_type"] == "foundry_dependency"
