from valuechain.canonicalization import canonical_merge_key
from valuechain.mention_layer import build_mention_clusters, extract_passage_mentions
from valuechain.models import Company, Passage


def passage(text: str) -> Passage:
    return Passage(
        passage_id="p-1", ticker="NVDA", cik="1045810", company_name="NVIDIA Corporation",
        form="10-K", accession_number="0001", filing_date="2026-01-01", accepted_timestamp="",
        source_document_url="https://www.sec.gov/example", section="Business", paragraph_offset=1,
        text=text, parser_name="test", parser_version="1",
    )


def test_mentions_keep_exact_spans_and_issuer_coreference():
    text = "We purchase memory from SK Hynix Inc., Micron Technology, Inc., and Samsung."
    rows = extract_passage_mentions(
        [passage(text)], [Company("NVDA", "NVIDIA Corporation"), Company("SSNLF", "Samsung Electronics Co., Ltd")]
    )
    samsung = next(row for row in rows if row.normalized_name == "Samsung Electronics Co., Ltd")
    issuer = next(row for row in rows if row.mention_kind == "issuer_reference")
    assert text[samsung.start_offset:samsung.end_offset] == "Samsung"
    assert samsung.resolution_status == "universe_alias"
    assert text[issuer.start_offset:issuer.end_offset] == "We"
    assert issuer.normalized_name == "NVIDIA Corporation"


def test_clusters_join_aliases_but_keep_unresolved_names():
    rows = extract_passage_mentions(
        [passage("TSMC and Taiwan Semiconductor Manufacturing Company Limited work with us.")],
        [Company("TSM", "Taiwan Semiconductor Manufacturing Company Limited"), Company("NVDA", "NVIDIA Corporation")],
    )
    clusters = build_mention_clusters(rows)
    tsmc = [row for row in rows if row.normalized_name == "Taiwan Semiconductor Manufacturing Company Limited"]
    assert len({row.cluster_id for row in tsmc}) == 1
    assert next(cluster for cluster in clusters if cluster.cluster_id == tsmc[0].cluster_id).mention_count == 2


def test_canonical_merge_key_does_not_merge_distinct_products_or_directions():
    memory = canonical_merge_key("supplier", "customer", "supplies_to", "current", "Memory", "supply_chain")
    wafer = canonical_merge_key("supplier", "customer", "supplies_to", "current", "Wafers", "supply_chain")
    reverse = canonical_merge_key("customer", "supplier", "supplies_to", "current", "Memory", "supply_chain")
    assert memory != wafer
    assert memory != reverse
