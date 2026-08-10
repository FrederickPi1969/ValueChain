from valuechain.financial_ie.models import DocumentChunk
from valuechain.financial_ie.retrieval import (
    BM25Index,
    chunk_pages,
    expand_query,
    financial_query_facets,
    focused_financial_search,
    include_anchor_chunks,
    rerank_with_embeddings,
    split_pdf_pages,
)


def test_chunk_pages_preserves_page_provenance_and_bounds() -> None:
    chunks = chunk_pages(["STATEMENT OF CASH FLOWS\n\n" + "revenue was 10. " * 200], max_chars=300)
    assert len(chunks) > 1
    assert all(chunk.page == 1 for chunk in chunks)
    assert all(len(chunk.text) <= 541 for chunk in chunks)


def test_query_expansion_retrieves_capex_synonym() -> None:
    chunks = [
        DocumentChunk("a", "General corporate background."),
        DocumentChunk("b", "Purchases of property plant and equipment were $500 million."),
    ]
    result = BM25Index(chunks).search("capital expenditure", limit=1)
    assert result[0].chunk_id == "b"
    assert "purchases of property" in expand_query("capital expenditure")


def test_embedding_rerank_combines_lexical_and_semantic_scores() -> None:
    candidates = [
        DocumentChunk("a", "cash flow", score=2.0),
        DocumentChunk("b", "capital investment", score=1.0),
    ]

    def embed(_: list[str]) -> list[list[float]]:
        return [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]

    result = rerank_with_embeddings("capex", candidates, embed, lexical_weight=0.2)
    assert result[0].chunk_id == "b"


def test_split_pdf_pages_preserves_blank_physical_pages() -> None:
    assert split_pdf_pages("first\f\fthird\f") == ["first", "", "third"]


def test_financial_query_facets_decompose_multi_input_metric() -> None:
    facets = financial_query_facets(
        "What is FY2021 inventory turnover, defined as COGS divided by average inventory?"
    )
    assert any("cost of goods sold" in facet for facet in facets)
    assert any("inventory inventories" in facet for facet in facets)
    assert all("2021" in facet for facet in facets)


def test_focused_search_retrieves_each_formula_component() -> None:
    chunks = [
        DocumentChunk("noise", "General business discussion for fiscal 2021."),
        DocumentChunk("cogs", "Cost of sales was 500 in 2021."),
        DocumentChunk("inventory", "Inventories were 100 in 2021."),
    ]
    ranked, anchors = focused_financial_search(
        BM25Index(chunks),
        "Calculate FY2021 inventory turnover from COGS and average inventory.",
    )
    ids = {chunk.chunk_id for chunk in [*ranked, *anchors]}
    assert {"cogs", "inventory"} <= ids


def test_anchor_inclusion_tracks_ids_removed_from_fixed_window() -> None:
    ranked = [DocumentChunk("a", "a"), DocumentChunk("b", "b")]
    anchors = [DocumentChunk("c", "c"), DocumentChunk("b", "b")]
    selected = include_anchor_chunks(ranked, anchors, limit=2)
    assert {chunk.chunk_id for chunk in selected} == {"b", "c"}
