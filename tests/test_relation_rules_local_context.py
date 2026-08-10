from valuechain.entity_resolution import EntityResolver
from valuechain.models import Passage
from valuechain.relation_rules import RuleBasedRelationExtractor, local_relation_context


def test_local_context_stops_before_unrelated_following_sentence() -> None:
    text = "We use foundries such as TSMC and Samsung. We purchase memory from SK Hynix, Micron, and Samsung."
    start = text.index("foundries")
    assert "SK Hynix" not in local_relation_context(text, start, start + 9)


def test_parallel_list_disaggregation_keeps_all_contract_manufacturers() -> None:
    passage = Passage(
        passage_id="p1", ticker="NVDA", company_name="NVIDIA Corporation", cik="1", form="10-K", accession_number="a",
        filing_date="2026-01-01", accepted_timestamp="", source_document_url="https://example.com", section="item_1",
        paragraph_offset=0, parser_name="test", parser_version="1",
        text="We engage with contract manufacturers such as Hon Hai Precision Industry Co., Ltd., Wistron Corporation, and Fabrinet to perform assembly.",
    )
    records = RuleBasedRelationExtractor(EntityResolver([])).extract(passage)
    names = {row.object for row in records if row.relation_type == "packaging_or_assembly_dependency"}
    assert {"Hon Hai Precision Industry Co., Ltd", "Wistron Corporation", "Fabrinet"} <= names


def test_parallel_list_disaggregation_handles_generic_purchase_action() -> None:
    passage = Passage(
        passage_id="p2", ticker="NVDA", company_name="NVIDIA Corporation", cik="1", form="10-K", accession_number="a",
        filing_date="2026-01-01", accepted_timestamp="", source_document_url="https://example.com", section="item_1",
        paragraph_offset=0, parser_name="test", parser_version="1",
        text="We purchase memory from SK Hynix Inc., Micron Technology, Inc., and Samsung.",
    )
    records = RuleBasedRelationExtractor(EntityResolver([])).extract(passage)
    names = {row.object for row in records if row.relation_type == "supplier_dependency"}
    assert {"SK Hynix Inc", "Micron Technology, Inc", "Samsung"} <= names


def test_plural_foundries_resolve_the_parallel_foundry_list() -> None:
    passage = Passage(
        passage_id="p3", ticker="NVDA", company_name="NVIDIA Corporation", cik="1", form="10-K", accession_number="a",
        filing_date="2026-01-01", accepted_timestamp="", source_document_url="https://example.com", section="item_1",
        paragraph_offset=0, parser_name="test", parser_version="1",
        text="We utilize foundries, such as Taiwan Semiconductor Manufacturing Company Limited and Samsung Electronics Co., Ltd., to produce semiconductor wafers.",
    )
    names = {row.object for row in RuleBasedRelationExtractor(EntityResolver([])).extract(passage) if row.relation_type == "foundry_dependency"}
    assert {"Taiwan Semiconductor Manufacturing Company Limited", "Samsung Electronics Co., Ltd"} <= names
