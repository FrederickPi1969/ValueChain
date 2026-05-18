from valuechain.models import Passage
from valuechain.relevance import filter_candidates, score_passage


def passage(text: str, section: str = "item_1a_risk_factors") -> Passage:
    return Passage(
        passage_id="p1",
        ticker="NVDA",
        cik="0001045810",
        company_name="NVIDIA Corporation",
        form="10-K",
        accession_number="0000000000-00-000000",
        filing_date="2025-02-26",
        accepted_timestamp="2025-02-26T00:00:00.000Z",
        source_document_url="https://example.com/filing.htm",
        section=section,
        paragraph_offset=0,
        text=text,
        parser_name="test",
        parser_version="0",
    )


def test_score_passage_finds_dependency_terms() -> None:
    result = score_passage(passage("We rely on a limited number of suppliers for foundry capacity."))
    assert result.relevance_score >= 7
    assert "rely on" in result.relevance_terms
    assert "foundry" in result.relevance_terms


def test_filter_candidates_drops_low_signal_passage() -> None:
    candidates = filter_candidates([passage("Revenue increased during the quarter.")])
    assert candidates == []

