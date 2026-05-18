from valuechain.filing_parser import chunk_text, html_table_rows_to_text, parse_sections, segment_passages, split_sections
from valuechain.models import SourceDocument


def test_split_sections_prefers_second_table_of_contents_match() -> None:
    text = (
        "Table of contents\nItem 1. Business\nItem 1A. Risk Factors\n"
        + "x" * 12000
        + "\nItem 1. Business\nWe sell accelerated computing platforms.\n"
        + "y" * 400
        + "\nItem 1A. Risk Factors\nWe rely on suppliers.\n"
        + "z" * 400
    )
    sections = split_sections(
        text,
        [
            ("item_1_business", r"\bitem\s+1[.\s:-]+business\b"),
            ("item_1a_risk_factors", r"\bitem\s+1a[.\s:-]+risk\s+factors\b"),
        ],
    )
    assert sections[0][0] == "item_1_business"
    assert "accelerated computing" in sections[0][1]


def test_split_sections_ignores_only_late_table_of_contents_matches() -> None:
    text = (
        "We are a global designer and manufacturer of semiconductor products. "
        "We source critical tools and materials from multiple suppliers. "
        + "x" * 12000
        + "\nItem 1. Business Pages 3-24\nItem 1A. Risk Factors Pages 37-51\n"
    )
    sections = split_sections(
        text,
        [
            ("item_1_business", r"\bitem\s+1[.\s:-]+business\b"),
            ("item_1a_risk_factors", r"\bitem\s+1a[.\s:-]+risk\s+factors\b"),
        ],
    )
    assert sections == []


def test_chunk_text_keeps_short_text_intact() -> None:
    assert chunk_text("A short paragraph.", max_chars=50) == ["A short paragraph."]


def test_exhibit_document_parses_as_exhibit_section_with_document_provenance(tmp_path) -> None:
    path = tmp_path / "q1exhibit991.htm"
    path.write_text(
        "<html><body><p>We entered into a strategic collaboration agreement with Example Partner Inc. "
        "for data center capacity and cloud services.</p></body></html>",
        encoding="utf-8",
    )
    document = SourceDocument(
        ticker="NET",
        cik="0001477333",
        company_name="Cloudflare Inc.",
        form="8-K",
        accession_number="0001477333-26-000033",
        filing_date="2026-05-07",
        document="q1exhibit991.htm",
        document_type="EX-99.1",
        document_url="https://www.sec.gov/Archives/example/q1exhibit991.htm",
        local_path=str(path),
    )
    sections = parse_sections(document)
    passages = segment_passages(sections[0])
    assert sections[0].section_name == "exhibit_99_1_investor_or_earnings"
    assert passages[0].source_document == "q1exhibit991.htm"
    assert passages[0].source_document_type == "EX-99.1"
    assert "q1exhibit991" in passages[0].passage_id


def test_exhibit_21_uses_table_rows_as_passages(tmp_path) -> None:
    path = tmp_path / "exhibit21.htm"
    path.write_text(
        "<html><body><table>"
        "<tr><th>Subsidiary</th><th>Jurisdiction</th></tr>"
        "<tr><td>Example Networks Limited</td><td>Ireland</td></tr>"
        "<tr><td>Example Systems LLC</td><td>Delaware</td></tr>"
        "</table></body></html>",
        encoding="utf-8",
    )
    document = SourceDocument(
        ticker="EX",
        cik="1",
        company_name="Example Inc.",
        form="10-K",
        accession_number="0000000000-26-000001",
        filing_date="2026-01-01",
        document="exhibit21.htm",
        document_type="EX-21",
        document_url="https://www.sec.gov/Archives/example/exhibit21.htm",
        local_path=str(path),
    )
    assert "Example Networks Limited | Ireland" in html_table_rows_to_text(path)
    sections = parse_sections(document)
    passages = segment_passages(sections[0])
    assert sections[0].section_name == "exhibit_21_subsidiaries"
    assert len(passages) == 2
