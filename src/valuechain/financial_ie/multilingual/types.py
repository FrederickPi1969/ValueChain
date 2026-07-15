from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from valuechain.financial_ie.models import DocumentChunk


@dataclass(frozen=True, slots=True)
class SourceDocument:
    source_id: str
    filing_id: str
    issuer_id: str
    issuer_name: str
    ticker: str
    language: str
    jurisdiction: str
    filing_type: str
    filing_type_raw: str
    title: str
    filed_at: str
    source_url: str
    manifest_path: Path
    document_path: Path
    document_granularity: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["manifest_path"] = str(self.manifest_path)
        row["document_path"] = str(self.document_path)
        return row


@dataclass(slots=True)
class ParsedDocument:
    source: SourceDocument
    chunks: list[DocumentChunk]
    parser_name: str
    parser_version: str
    warnings: list[str] = field(default_factory=list)
    source_character_count: int = 0


@dataclass(frozen=True, slots=True)
class LanguagePack:
    code: str
    native_name: str
    profile_queries: tuple[str, ...]
    signal_queries: tuple[tuple[str, str], ...]
    section_cues: tuple[tuple[str, tuple[str, ...]], ...]
    hypothetical_markers: tuple[str, ...]
    forward_markers: tuple[str, ...]
