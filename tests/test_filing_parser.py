from valuechain.filing_parser import chunk_text, split_sections


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


def test_chunk_text_keeps_short_text_intact() -> None:
    assert chunk_text("A short paragraph.", max_chars=50) == ["A short paragraph."]

