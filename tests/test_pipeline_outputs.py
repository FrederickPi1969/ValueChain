import csv
import asyncio

from valuechain.models import RelationEvidence
from valuechain.pipeline import extract_relations_async, write_validation_sample
from valuechain.models import Passage


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


def test_extract_relations_async_respects_concurrency() -> None:
    class AsyncExtractor:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.closed = False

        async def extract_async(self, passage):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return []

        async def aclose(self):
            self.closed = True

    passages = [
        Passage(
            passage_id=f"p{i}",
            ticker="T",
            cik="1",
            company_name="Test Co",
            form="10-K",
            accession_number="a1",
            filing_date="2026-01-01",
            accepted_timestamp="",
            source_document_url="https://example.com",
            section="item_1_business",
            paragraph_offset=i,
            text="We rely on suppliers.",
            parser_name="parser",
            parser_version="0.1",
        )
        for i in range(5)
    ]
    extractor = AsyncExtractor()
    records = asyncio.run(extract_relations_async(passages, extractor, concurrency=2))
    assert records == []
    assert extractor.max_active <= 2
    assert extractor.closed is True
