from __future__ import annotations

import re
import unicodedata


ELLIPSIS_RE = re.compile(r"(?:\.{3,}|…+)")


def normalize_match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character)[0] in {"L", "N", "S"}
    )


def quote_in_text(quote: str, text: str) -> bool:
    normalized_text = normalize_match_text(text)
    if not normalized_text:
        return False
    if ELLIPSIS_RE.search(quote):
        cursor = 0
        matched = False
        for raw_part in ELLIPSIS_RE.split(quote):
            part = normalize_match_text(raw_part)
            if not part:
                continue
            position = normalized_text.find(part, cursor)
            if position < 0:
                return False
            matched = True
            cursor = position + len(part)
        return matched
    normalized_quote = normalize_match_text(quote)
    return bool(normalized_quote and normalized_quote in normalized_text)


def evidence_failure_reason(chunk_exists: bool, quote: str, source_text: str) -> str:
    if not chunk_exists:
        return "unknown_chunk_id"
    if not quote.strip():
        return "empty_quote"
    if not quote_in_text(quote, source_text):
        return "quote_not_found"
    return ""
