"""Stable identifiers for individual relation assertions.

``passage_id`` identifies a source-text span.  A single passage can support
several counterparties or relationship types, so it must not double as the
identifier for an extracted assertion.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any


def stable_evidence_id(record: Any) -> str:
    """Return an assertion-level ID, preserving an explicitly stored value."""
    explicit = value(record, "evidence_id")
    if explicit:
        return explicit
    fields = (
        value(record, "passage_id"),
        value(record, "subject").casefold(),
        value(record, "relation_type").casefold(),
        value(record, "object").casefold(),
        value(record, "direction").casefold(),
    )
    return f"evidence:{sha256('|'.join(fields).encode('utf-8')).hexdigest()[:20]}"


def value(record: Any, key: str) -> str:
    if isinstance(record, dict):
        return str(record.get(key, "")).strip()
    return str(getattr(record, key, "")).strip()
