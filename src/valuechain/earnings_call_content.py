"""Deterministic content guards for earnings-call acquisition."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass

_OPERATOR_CONCLUSION_PATTERN = (
    r"\bthis concludes[^\n]{0,200}\b(?:conference|call|webcast)\b"
)
_MANAGEMENT_CONCLUSION_PATTERN = (
    r"\b(?:goodbye for now|thanks for spending time with us|"
    r"we(?:'ll| will) wrap up (?:there|here))\b"
)
_BRINGS_END_PATTERN = (
    r"\bthis (?:does )?bring(?:s)? us to the end of (?:the )?"
    r"(?:meeting|call|conference|presentation)\b"
)
_CAPTION_TIMING_PREFIX_PATTERN = (
    r"(?m)^\s*(?:\d{1,2}:)?\d{2}:\d{2}(?:[.,]\d+)?\s*-->\s*"
    r"(?:\d{1,2}:)?\d{2}:\d{2}(?:[.,]\d+)?\s*(?:\|\s*)?"
)


@dataclass(frozen=True)
class OpenCLIExtractChunk:
    content: str
    start: int
    end: int
    next_start_char: int | None
    total_chars: int


def normalize_transcript_text(raw: str) -> str:
    """Turn scraper/browser output into stable plain text for validation.

    ULSCAR normally returns readable text, while Browser Bridge and unusual IR
    pages can still contain HTML shells.  The downloader applies this before it
    persists anything so validators never need to reason over script/style
    residue.
    """
    if raw.lstrip().startswith("%PDF"):
        raise ValueError("binary PDF reached text post-processing")
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head).*?>.*?</\1>", " ", raw)
    raw = html.unescape(raw)
    # OpenCLI emits Markdown and can inline page images as enormous data URIs.
    # Remove the payload before processing Markdown so base64 never reaches the
    # validator or compressed corpus.
    raw = re.sub(
        r"(?i)data:image/[a-z0-9.+-]+(?:;[a-z0-9=.+-]+)*;base64,[a-z0-9_+/=-]{80,}",
        " ",
        raw,
    )
    # Legacy extractors inserted a newline at fixed character cursors, which
    # can strand the middle of a data URI without its `data:image` prefix.
    # Natural-language transcripts do not contain 512-character base64 tokens.
    raw = re.sub(r"(?<![\w/+])[A-Za-z0-9+/]{512,}={0,2}(?![\w/+])", " ", raw)
    raw = re.sub(r"(?s)!\[[^\]\r\n]*\]\(.{0,2000000}?\)", " ", raw)
    raw = re.sub(r"!\[[^\]\n]*\]\[[^\]\n]*\]", " ", raw)
    # Preserve human-readable anchor labels but discard navigation URLs.
    raw = re.sub(r"(?<!!)\[([^\]\n]{1,500})\]\([^\n)]*\)", r"\1", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = raw.replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
    text = "\n".join(line for line in lines if line)
    # Browser transcript pages often append subscription promos, audio-player
    # notices, or publisher disclaimers after the operator has explicitly
    # ended the call.  Keep the authoritative call boundary and discard the
    # site chrome that follows it.
    tail_start = max(0, len(text) - 12_000)
    end_markers = list(
        re.finditer(
            r"(?i)\b(?:you may (?:now )?disconnect|"
            + _OPERATOR_CONCLUSION_PATTERN
            + "|"
            + _MANAGEMENT_CONCLUSION_PATTERN
            + "|"
            + _BRINGS_END_PATTERN
            + r")[.!]?",
            text[tail_start:],
        )
    )
    if end_markers:
        text = text[: tail_start + end_markers[-1].end()].rstrip()
    return text


def transcript_quality_metrics(text: str) -> dict[str, int | float]:
    """Return cheap, deterministic signals for downloader quality gates."""
    characters = len(text)
    lines = [line for line in text.splitlines() if line.strip()]
    words = re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE)
    alphanumeric = sum(character.isalnum() for character in text)
    data_uris = len(re.findall(r"(?i)data:image/|;base64,", text))
    markdown_images = len(re.findall(r"!\[[^\]]*\]\(", text))
    base64_runs = len(re.findall(r"(?<![\w/+])[A-Za-z0-9+/]{256,}={0,2}(?![\w/+])", text))
    seen: set[str] = set()
    duplicate_chars = 0
    substantive_chars = 0
    for line in lines:
        key = re.sub(r"\s+", " ", line).strip().lower()
        if len(key) < 40:
            continue
        substantive_chars += len(line)
        if key in seen:
            duplicate_chars += len(line)
        else:
            seen.add(key)
    return {
        "characters": characters,
        "lines": len(lines),
        "words": len(words),
        "alphanumeric_ratio": round(alphanumeric / max(characters, 1), 4),
        "duplicate_line_ratio": round(duplicate_chars / max(substantive_chars, 1), 4),
        "data_uri_markers": data_uris,
        "markdown_images": markdown_images,
        "base64_runs": base64_runs,
        "longest_line": max((len(line) for line in lines), default=0),
    }


def transcript_quality_problems(text: str) -> tuple[str, ...]:
    """Identify extraction failures before an artifact is accepted as downloaded."""
    metrics = transcript_quality_metrics(text)
    problems: list[str] = []
    if metrics["characters"] < 500:
        problems.append("fewer than 500 readable characters")
    if metrics["words"] < 75:
        problems.append("fewer than 75 word-like tokens")
    if metrics["alphanumeric_ratio"] < 0.15:
        problems.append("mostly non-text characters")
    if metrics["data_uri_markers"] or metrics["markdown_images"] or metrics["base64_runs"]:
        problems.append("embedded image/data-URI residue")
    return tuple(problems)


def parse_opencli_extract_chunk(raw: str) -> OpenCLIExtractChunk:
    """Parse one ``opencli browser extract`` page and validate its cursor."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("OpenCLI extract response was not an object")
    content = payload.get("content")
    if not isinstance(content, str):
        raise TypeError("OpenCLI extract response did not contain text")
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
    ("operator_conclusion", _OPERATOR_CONCLUSION_PATTERN),
    ("management_conclusion", _MANAGEMENT_CONCLUSION_PATTERN),
    ("brings_end", _BRINGS_END_PATTERN),
    ("disconnect", r"\byou may (?:now )?disconnect\b"),
    ("explicit_close", r"\bwe close (?:our|the) .{0,40}\bcall\b"),
    ("concludes_remarks", r"\bthat concludes (?:our|the) .{0,60}(?:call|remarks|presentation)\b"),
    ("end_marker", r"\bend of (?:the )?(?:conference|call|transcript|q&a)\b"),
    ("closing_thanks", r"\bthank you(?: very much)?(?:,? everyone|,? everybody| all)? for (?:your )?(?:time|participation|joining|interest)\b"),
)


def transcript_closing_signals(text: str) -> tuple[str, ...]:
    """Return explicit call-ending signals found near the document boundary."""
    # YouTube/VTT captions can split one sentence across timestamped lines.
    # Collapsing whitespace preserves the wording while letting the same
    # bounded closing patterns work for prose and caption transcripts.
    tail = re.sub(_CAPTION_TIMING_PREFIX_PATTERN, " ", text[-12_000:].lower())
    tail = re.sub(r"\s+", " ", tail)
    return tuple(name for name, pattern in _CLOSING_PATTERNS if re.search(pattern, tail, re.IGNORECASE | re.DOTALL))


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
    wrong-period verdict into a full transcript.  Bounded source-aware
    exceptions cover substantive official conference PDFs and complete
    YouTube caption files when the model underrates an explicit closing.
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
    if source_kind == "youtube" and content_kind == "youtube_video":
        # A transcript-service response is bounded by the video's captions.
        # Models sometimes mistake overlapping auto-caption lines for an
        # appended fragment even though the actual tail contains the closing.
        lower = text.lower()
        qa_present = any(
            marker in lower
            for marker in (
                "question-and-answer",
                "questions and answers",
                "q&a",
                "question",
                "analyst",
            )
        )
        bounded_video = (
            verdict.get("period_match") is True
            and verdict.get("document_type") in {"earnings_call", "partial_call"}
            and float(verdict.get("confidence", 0)) >= confidence_floor
            and len(text) >= 7_000
            and qa_present
            and bool(transcript_closing_signals(text))
        )
        return exact_full_call or bounded_video
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
