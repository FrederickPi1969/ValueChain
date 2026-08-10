import json

import pytest

from valuechain.earnings_call_content import (
    parse_opencli_extract_chunk,
    transcript_closing_signals,
    transcript_is_complete,
)


def test_opencli_extract_chunk_preserves_pagination_cursor() -> None:
    chunk = parse_opencli_extract_chunk(json.dumps({
        "content": "a" * 20_000,
        "start": 0,
        "end": 20_000,
        "next_start_char": 20_000,
        "total_chars": 54_321,
    }))
    assert chunk.next_start_char == 20_000
    assert chunk.total_chars == 54_321


def test_opencli_extract_chunk_rejects_silent_truncation() -> None:
    with pytest.raises(ValueError, match="ended before"):
        parse_opencli_extract_chunk(json.dumps({
            "content": "partial",
            "start": 0,
            "end": 7,
            "next_start_char": None,
            "total_chars": 100,
        }))


@pytest.mark.parametrize("override", [
    {"full_call": False, "document_type": "partial_call"},
    {"period_match": False},
    {"document_type": "filing"},
    {"confidence": 0.79},
])
def test_transcript_gate_never_promotes_invalid_verdicts(override: dict) -> None:
    verdict = {
        "full_call": True,
        "period_match": True,
        "document_type": "earnings_call",
        "confidence": 0.95,
    } | override
    assert not transcript_is_complete(
        verdict,
        "Operator: This concludes today's conference call. You may now disconnect.",
        source_kind="web",
        content_kind="third_party_transcript",
    )


def test_transcript_gate_accepts_exact_period_full_call() -> None:
    assert transcript_is_complete({
        "full_call": True,
        "period_match": True,
        "document_type": "earnings_call",
        "confidence": 0.90,
    }, "Operator: This concludes today's conference call.", source_kind="web", content_kind="third_party_transcript")


def test_web_transcript_requires_a_real_ending() -> None:
    verdict = {
        "full_call": True,
        "period_match": True,
        "document_type": "earnings_call",
        "confidence": 0.95,
    }
    assert not transcript_is_complete(
        verdict,
        "Analyst: My question is about margins. CEO: We expect",
        source_kind="web",
        content_kind="third_party_transcript",
    )


def test_closing_signal_detects_explicit_ir_close() -> None:
    text = "Thank you very much. With that, we close our Q1 call, and have a nice day."
    assert "explicit_close" in transcript_closing_signals(text)


def test_official_conference_pdf_can_use_its_file_boundary() -> None:
    verdict = {
        "full_call": False,
        "period_match": True,
        "document_type": "partial_call",
        "confidence": 0.90,
    }
    text = ("Analyst: question? Management: answer. " * 250)
    assert transcript_is_complete(
        verdict,
        text,
        source_kind="pdf",
        content_kind="official_transcript",
    )
