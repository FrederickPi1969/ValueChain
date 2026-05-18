from valuechain.entity_resolution import EntityResolver
from valuechain.models import Company, Passage
from valuechain.relation_rules import RuleBasedRelationExtractor, infer_modality


def make_passage(text: str, section: str = "item_1_business") -> Passage:
    return Passage(
        passage_id="p1",
        ticker="NVDA",
        cik="0001045810",
        company_name="NVIDIA Corporation",
        form="10-K",
        accession_number="0001045810-25-000023",
        filing_date="2025-02-26",
        accepted_timestamp="2025-02-26T00:00:00.000Z",
        source_document_url="https://www.sec.gov/example.htm",
        section=section,
        paragraph_offset=3,
        text=text,
        parser_name="parser",
        parser_version="0.1",
    )


def test_rules_extract_current_foundry_dependency_with_resolved_object() -> None:
    companies = [
        Company("NVDA", "NVIDIA Corporation", cik="0001045810"),
        Company("TSM", "Taiwan Semiconductor Manufacturing Company Limited", cik="0001046179"),
    ]
    extractor = RuleBasedRelationExtractor(EntityResolver(companies))
    records = extractor.extract(
        make_passage("We rely on supplier TSMC for foundry and wafer fabrication capacity.")
    )
    assert {record.relation_type for record in records} >= {"foundry_dependency", "supplier_dependency"}
    foundry = next(record for record in records if record.relation_type == "foundry_dependency")
    assert foundry.object == "Taiwan Semiconductor Manufacturing Company Limited"
    assert foundry.modality == "current_fact"


def test_risk_language_is_not_current_fact_by_default() -> None:
    modality = infer_modality(
        "item_1a_risk_factors",
        "our suppliers may be unable to provide capacity, which could adversely affect us",
    )
    assert modality == "risk_hypothetical"


def test_rules_do_not_emit_subject_as_dependency_object() -> None:
    companies = [
        Company("MSFT", "Microsoft Corporation", cik="0000789019"),
        Company("AMZN", "Amazon.com Inc.", cik="0001018724"),
    ]
    extractor = RuleBasedRelationExtractor(EntityResolver(companies))
    records = extractor.extract(
        make_passage(
            "Microsoft Corporation relies on cloud infrastructure and data center capacity.",
            section="item_1_business",
        )
    )
    assert records
    assert all(record.object != "Microsoft Corporation" for record in records)


def test_rules_extract_multiple_named_subcontractors_from_list() -> None:
    companies = [Company("NVDA", "NVIDIA Corporation", cik="0001045810")]
    extractor = RuleBasedRelationExtractor(EntityResolver(companies))
    records = extractor.extract(
        make_passage(
            "We engage with independent subcontractors and contract manufacturers such as "
            "Hon Hai Precision Industry Co., Ltd., Wistron Corporation, and Fabrinet to "
            "perform assembly, testing and packaging of our final products."
        )
    )
    packaging_objects = {record.object for record in records if record.relation_type == "packaging_or_assembly_dependency"}
    assert "Hon Hai Precision Industry Co" in packaging_objects or "Hon Hai Precision Industry Co., Ltd" in packaging_objects
    assert "Wistron Corporation" in packaging_objects
    assert "Fabrinet" in packaging_objects
