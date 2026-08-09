from valuechain.models import Passage
from valuechain.relation_llm import LLMRelationExtractor, merge_relation_records, normalize_object_payload, records_from_payload, should_skip_llm


def test_normalize_object_payload_accepts_structured_llm_object() -> None:
    assert normalize_object_payload({"name": "Customer", "type": "Generic"}) == "Customer"


def test_normalize_object_payload_rejects_empty_structured_object() -> None:
    assert normalize_object_payload({"type": "Generic"}) == ""


def test_records_from_payload_rejects_relation_type_as_object() -> None:
    records = records_from_payload(
        sample_passage(),
        "test-model",
        [
            {
                "object": "manufacturing_dependency",
                "relation_type": "manufacturing_dependency",
                "modality": "current_fact",
                "confidence_score": 0.9,
            }
        ],
    )
    assert records == []


def test_records_from_payload_accepts_generic_customer_object_for_recall() -> None:
    records = records_from_payload(
        sample_passage(),
        "test-model",
        [
            {
                "object": "Customer",
                "relation_type": "customer_dependency",
                "modality": "risk_hypothetical",
                "confidence_score": 0.9,
            }
        ],
    )
    assert len(records) == 1
    assert records[0].object == "Customer"


def test_records_from_payload_accepts_specific_named_relation_and_clamps_confidence() -> None:
    records = records_from_payload(
        sample_passage(),
        "test-model",
        [
            {
                "object": "Taiwan Semiconductor Manufacturing Company Limited",
                "relation_type": "foundry_dependency",
                "modality": "current_fact",
                "confidence_score": 1.4,
            }
        ],
    )
    assert len(records) == 1
    assert records[0].object == "Taiwan Semiconductor Manufacturing Company Limited"
    assert records[0].confidence_score == 1.0


def test_records_from_payload_retains_product_as_relationship_metadata() -> None:
    records = records_from_payload(
        sample_passage(), "test-model", [{
            "object": "SK Hynix Inc.", "relation_type": "supplier_dependency", "modality": "current_fact",
            "confidence_score": 0.9, "product_or_service": "memory",
        }]
    )
    assert records[0].product_or_service == "memory"


def test_hybrid_merge_uses_llm_to_enrich_rule_fact_fields() -> None:
    rule = records_from_payload(sample_passage(), "rules", [{"object": "SK Hynix Inc.", "relation_type": "supplier_dependency", "modality": "current_fact", "confidence_score": .7}])[0]
    llm = records_from_payload(sample_passage(), "llm", [{"object": "SK Hynix Inc.", "relation_type": "supplier_dependency", "modality": "current_fact", "confidence_score": .9, "product_or_service": "memory", "evidence_quote": "purchase memory from SK Hynix", "direction_candidate": "object_to_subject"}])[0]
    merged = merge_relation_records([rule], [llm])[0]
    assert merged.product_or_service == "memory"
    assert merged.direction_candidate == "object_to_subject"


def test_records_from_payload_rejects_invalid_schema_values() -> None:
    records = records_from_payload(
        sample_passage(),
        "test-model",
        [
            {
                "object": "TSMC",
                "relation_type": "vendor_relationship",
                "modality": "current_fact",
                "confidence_score": 0.9,
            },
            {
                "object": "TSMC",
                "relation_type": "foundry_dependency",
                "modality": "present",
                "confidence_score": 0.9,
            },
        ],
    )
    assert records == []


def test_records_from_payload_rejects_strategic_relation_without_strategic_modality() -> None:
    records = records_from_payload(
        sample_passage(),
        "test-model",
        [
            {
                "object": "Broadcom Inc.",
                "relation_type": "strategic_partner",
                "modality": "current_fact",
                "confidence_score": 0.9,
            }
        ],
    )
    assert records == []


def test_llm_extractor_returns_empty_records_on_client_failure() -> None:
    extractor = LLMRelationExtractor(FailingClient(), model_version="test-model")
    assert extractor.extract(sample_passage()) == []


def test_hybrid_skips_llm_for_exhibit_21_table_rows() -> None:
    passage = sample_passage()
    passage.section = "exhibit_21_subsidiaries"
    passage.source_document_type = "EX-21"
    assert should_skip_llm(passage) is True


def sample_passage() -> Passage:
    return Passage(
        passage_id="p1",
        ticker="AMD",
        cik="0000002488",
        company_name="Advanced Micro Devices Inc.",
        form="10-K",
        accession_number="a1",
        filing_date="2026-01-01",
        accepted_timestamp="",
        source_document_url="https://example.com",
        section="item_1_business",
        paragraph_offset=0,
        text="We rely on Taiwan Semiconductor Manufacturing Company Limited for wafer fabrication.",
        parser_name="parser",
        parser_version="0.1",
    )


class FailingClient:
    def chat_json(self, system: str, user: str):
        raise ValueError("malformed model output")
