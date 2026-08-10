"""Deterministic content guards for earnings-call acquisition."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class OpenCLIExtractChunk:
    content: str
    start: int
    end: int
    next_start_char: int | None
    total_chars: int


def parse_opencli_extract_chunk(raw: str) -> OpenCLIExtractChunk:
    """Parse one ``opencli browser extract`` page and validate its cursor."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("OpenCLI extract response was not an object")
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError("OpenCLI extract response did not contain text")
    start = int(payload.get("start", 0))
    end = int(payload.get("end", start + len(content)))
    total = int(payload.get("total_chars", end))
    next_value = payload.get("next_start_char")
    next_start = None if next_value is None else int(next_value)
    if start < 0 or end < start or total < end:
        raise ValueError("OpenCLI extract response contained invalid offsets")
    if next_start is not None and next_start <= start:
        raise ValueError("OpenCLI extract cursor did not advance")
    if next_start is None and end < total:
        raise ValueError("OpenCLI extract ended before total_chars")
    return OpenCLIExtractChunk(content, start, end, next_start, total)


_CLOSING_PATTERNS = (
    ("operator_conclusion", r"\bthis concludes (?:today'?s|the) (?:conference|call)\b"),
    ("disconnect", r"\byou may (?:now )?disconnect\b"),
    ("explicit_close", r"\bwe close (?:our|the) .{0,40}\bcall\b"),
    ("concludes_remarks", r"\bthat concludes (?:our|the) .{0,60}(?:call|remarks|presentation)\b"),
    ("end_marker", r"\bend of (?:the )?(?:conference|call|transcript|q&a)\b"),
    ("closing_thanks", r"\bthank you(?: very much)?(?:,? everyone|,? everybody| all)? for (?:your )?(?:time|participation|joining|interest)\b"),
)


def transcript_closing_signals(text: str) -> tuple[str, ...]:
    """Return explicit call-ending signals found near the document boundary."""
    tail = text[-12_000:].lower()
    return tuple(name for name, pattern in _CLOSING_PATTERNS if re.search(pattern, tail, re.I | re.S))


def transcript_is_complete(
    verdict: dict,
    text: str,
    *,
    source_kind: str,
    content_kind: str,
    confidence_floor: float = 0.80,
) -> bool:
    """Apply non-negotiable gates after the model reviews the document.

    A deterministic rule must never turn a scraped web ``partial_call`` or a
    wrong-period verdict into a full transcript.  The sole bounded exception
    is a substantive, period-matched official conference PDF: its file
    boundary is authoritative even when it lacks an operator sign-off.
    """
    exact_full_call = (
        verdict.get("full_call") is True
        and verdict.get("period_match") is True
        and verdict.get("document_type") == "earnings_call"
        and float(verdict.get("confidence", 0)) >= confidence_floor
    )
    if source_kind == "web":
        # Browser/scraper output is not a file boundary.  Require an explicit
        # call ending so a genuine but first-20k excerpt cannot pass.
        return exact_full_call and bool(transcript_closing_signals(text))
    if source_kind == "pdf" and content_kind == "official_transcript":
        # Official conference PDFs are bounded source files and do not always
        # contain an operator sign-off (SMFG is a known example).  Permit the
        # model's partial_call label only for substantive, period-matched Q&A.
        lower = text.lower()
        qa_present = any(marker in lower for marker in ("question-and-answer", "questions and answers", "q&a", "question:", "analyst"))
        official_conference = (
            verdict.get("period_match") is True
            and verdict.get("document_type") in {"earnings_call", "partial_call"}
            and float(verdict.get("confidence", 0)) >= confidence_floor
            and len(text) >= 7_000
            and qa_present
        )
        return exact_full_call or official_conference
    return exact_full_call
