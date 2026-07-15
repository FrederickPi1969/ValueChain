from valuechain.financial_ie.models import DocumentChunk
from valuechain.financial_ie.multilingual.languages import get_language_pack
from valuechain.financial_ie.multilingual.retrieval import (
    MultilingualBM25,
    select_signal_chunks,
    tokenize,
)
from valuechain.financial_ie.multilingual.types import ParsedDocument, SourceDocument


def test_tokenizer_retains_cjk_and_hangul_terms() -> None:
    assert "供应" in tokenize("主要供应商集中度", "zh-Hans")
    assert "売上" in tokenize("売上高が増加", "ja")
    assert "공급" in tokenize("주요 공급업체", "ko")


def test_bm25_finds_japanese_supplier_passage() -> None:
    chunks = [
        DocumentChunk("generic", "当社の沿革について説明します。"),
        DocumentChunk("supplier", "主要な原材料は特定の仕入先から調達しています。"),
    ]
    results = MultilingualBM25(chunks, "ja").search("主要な仕入先 調達")
    assert results[0].chunk_id == "supplier"


def test_signal_selection_preserves_korean_supply_chain_chunk(tmp_path) -> None:
    source = SourceDocument(
        source_id="opendart",
        filing_id="1",
        issuer_id="1",
        issuer_name="테스트",
        ticker="000001",
        language="ko",
        jurisdiction="KR",
        filing_type="annual_report",
        filing_type_raw="사업보고서",
        title="사업보고서",
        filed_at="2025-01-01",
        source_url="https://example.test",
        manifest_path=tmp_path / "filing.json",
        document_path=tmp_path / "filing.zip",
        document_granularity="periodic_report",
    )
    parsed = ParsedDocument(
        source=source,
        chunks=[
            DocumentChunk("generic", "회사의 연혁입니다."),
            DocumentChunk(
                "supply",
                "핵심 원재료는 소수의 주요 공급업체에서 조달합니다.",
                section_hint="supply_chain",
            ),
        ],
        parser_name="fixture",
        parser_version="1",
    )
    selected = select_signal_chunks(parsed, get_language_pack("ko"))
    assert "supply" in {chunk.chunk_id for chunk in selected}
