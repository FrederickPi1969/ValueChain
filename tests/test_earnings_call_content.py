import json

import pytest

from valuechain.earnings_call_content import (
    normalize_transcript_text,
    parse_opencli_extract_chunk,
    transcript_closing_signals,
    transcript_is_complete,
    transcript_quality_metrics,
    transcript_quality_problems,
)


def test_normalization_removes_inline_and_cursor_split_base64_images() -> None:
    payload = "A" * 40_000
    # Simulate the legacy fixed-cursor newline in the middle of a data URI.
    polluted = (
        "Operator: Welcome to the first quarter earnings call.\n"
        "![avatar](data:image/png;base64,"
        + payload[:20_000]
        + "\n"
        + payload[20_000:]
        + ")\nChief Executive Officer: Prepared remarks.\n"
        + ("Analyst: Question. Executive: Answer.\n" * 100)
    )
    cleaned = normalize_transcript_text(polluted)
    metrics = transcript_quality_metrics(cleaned)
    assert "data:image" not in cleaned
    assert payload[:1_000] not in cleaned
    assert metrics["data_uri_markers"] == 0
    assert metrics["markdown_images"] == 0
    assert metrics["base64_runs"] == 0
    assert transcript_quality_problems(cleaned) == ()


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


def test_operator_close_allows_company_and_period_between_concludes_and_call() -> None:
    text = (
        "Operator: This concludes the KLA Corporation December Quarter 2025 "
        "earnings call and webcast. Please disconnect your line."
    )
    assert "operator_conclusion" in transcript_closing_signals(text)


def test_normalization_trims_chrome_after_named_operator_close() -> None:
    raw = (
        "Operator: Welcome to the earnings call.\n"
        + ("Executive: Prepared remarks and analyst discussion.\n" * 100)
        + "Operator: This concludes the KLA Corporation December Quarter 2025 "
        "earnings call and webcast.\n"
        + "Disclaimer: publisher material.\nMore transcripts\n"
    )
    cleaned = normalize_transcript_text(raw)
    assert cleaned.endswith("webcast.")
    assert "publisher material" not in cleaned
    assert "More transcripts" not in cleaned


def test_management_goodbye_is_a_bounded_call_ending_and_trims_disclaimer() -> None:
    raw = (
        "Operator: Welcome.\n"
        + ("Management remarks and analyst question and answer.\n" * 150)
        + "We thank all shareholders. Thanks for spending time with us and "
        "we look forward to our next update. Goodbye for now.\n"
        + "Disclaimer: This transcript was computer generated.\n"
        + "Your browser does not support the audio element."
    )
    cleaned = normalize_transcript_text(raw)
    assert cleaned.endswith("Goodbye for now.")
    assert "management_conclusion" in transcript_closing_signals(cleaned)
    assert "computer generated" not in cleaned


def test_normalization_trims_publisher_chrome_after_operator_close() -> None:
    raw = (
        "Operator: Welcome to the earnings call.\n"
        + ("Executive: Prepared remarks and analyst discussion.\n" * 100)
        + "Operator: This concludes today's conference call. You may now disconnect.\n"
        + "Professional-grade tools for investors. Try for free.\n"
        + "Your browser does not support the audio element."
    )
    cleaned = normalize_transcript_text(raw)
    assert cleaned.endswith("You may now disconnect.")
    assert "Try for free" not in cleaned
    assert "audio element" not in cleaned


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


def test_complete_youtube_captions_survive_model_partial_false_negative() -> None:
    verdict = {
        "full_call": False,
        "period_match": True,
        "document_type": "partial_call",
        "confidence": 0.95,
    }
    text = (
        "00:00 | Welcome. Safe harbor. Analyst question and management answer.\n"
        * 150
        + "02:34 | Thank you everyone for joining us for our earnings call. "
        "We look forward to seeing you in three months."
    )
    assert transcript_is_complete(
        verdict,
        text,
        source_kind="youtube",
        content_kind="youtube_video",
    )


def test_youtube_closing_signal_can_span_caption_lines() -> None:
    text = (
        "02:34:43.200 --> 02:34:47.200 | Thank\n"
        "02:34:45.680 --> 02:34:49.120 | you everyone for joining us for our\n"
        "02:34:49.120 --> 02:34:55.439 | earnings call and see you in 3 months."
    )
    assert "closing_thanks" in transcript_closing_signals(text)


def test_caption_closing_detects_bringing_meeting_to_an_end() -> None:
    text = (
        "00:56:29.200 --> 00:56:33.520 | This does bring us to the end of the\n"
        "00:56:30.559 --> 00:56:33.520 | meeting and you may"
    )
    assert "brings_end" in transcript_closing_signals(text)


def test_youtube_partial_without_closing_stays_rejected() -> None:
    verdict = {
        "full_call": False,
        "period_match": True,
        "document_type": "partial_call",
        "confidence": 0.95,
    }
    text = "Analyst question and management answer continue.\n" * 250
    assert not transcript_is_complete(
        verdict,
        text,
        source_kind="youtube",
        content_kind="youtube_video",
    )
